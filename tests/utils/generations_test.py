from dataclasses import dataclass

import anyio
import pytest

from oauthclientbridge.utils.generations import Generations


@dataclass(frozen=True)
class Resource:
    name: str


@pytest.mark.anyio
async def test_generations_finalize_retired_value_after_its_last_lease_releases() -> (
    None
):
    resources = iter((Resource("first"), Resource("second")))
    finalized: list[Resource] = []

    async def finalize(resource: Resource) -> None:
        finalized.append(resource)

    async with Generations(lambda: next(resources), finalize) as generations:
        async with generations.acquire() as first_lease:
            async with generations.acquire() as second_lease:
                assert first_lease.value == Resource("first")
                assert second_lease.value == Resource("first")

                await generations.rotate(first_lease)

                assert first_lease.value == Resource("second")
                assert second_lease.value == Resource("first")

            assert finalized == [Resource("first")]

        assert finalized == [Resource("first")]

    assert finalized == [Resource("first"), Resource("second")]


@pytest.mark.anyio
async def test_generations_observes_current_and_retired_leases_and_retired_drains() -> (
    None
):
    resources = iter((Resource("first"), Resource("second")))
    lease_counts: list[tuple[int, int]] = []
    drain_durations: list[float] = []

    async def finalize(_resource: Resource) -> None:
        pass

    async with Generations(
        lambda: next(resources),
        finalize,
        on_leases_changed=lambda current, retired: lease_counts.append(
            (current, retired)
        ),
        on_retired_drain=drain_durations.append,
    ) as generations:
        async with generations.acquire() as first_lease:
            async with generations.acquire():
                await generations.rotate(first_lease)

    assert lease_counts == [(0, 0), (1, 0), (2, 0), (1, 1), (1, 0), (0, 0)]
    assert len(drain_durations) == 1
    assert drain_durations[0] >= 0


@pytest.mark.anyio
async def test_generations_finalize_current_value_once() -> None:
    finalized: list[Resource] = []

    async def finalize(resource: Resource) -> None:
        finalized.append(resource)

    generations = Generations(lambda: Resource("current"), finalize)

    async with generations:
        pass
    await generations.aclose()

    assert finalized == [Resource("current")]


@pytest.mark.anyio
async def test_generations_rotate_stale_lease_to_current_value() -> None:
    resources = iter((Resource("first"), Resource("second")))
    finalized: list[Resource] = []

    async def finalize(resource: Resource) -> None:
        finalized.append(resource)

    async with Generations(lambda: next(resources), finalize) as generations:
        async with generations.acquire() as first_lease:
            async with generations.acquire() as second_lease:
                await generations.rotate(first_lease)
                await generations.rotate(second_lease)

                assert first_lease.value == Resource("second")
                assert second_lease.value == Resource("second")

    assert finalized == [Resource("first"), Resource("second")]


@pytest.mark.anyio
async def test_generations_rotate_concurrent_leases_once() -> None:
    resources = iter((Resource("first"), Resource("second")))

    async def finalize(_resource: Resource) -> None:
        pass

    async with Generations(lambda: next(resources), finalize) as generations:
        async with generations.acquire() as first_lease:
            async with generations.acquire() as second_lease:
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(generations.rotate, first_lease)
                    task_group.start_soon(generations.rotate, second_lease)

                assert first_lease.value == Resource("second")
                assert second_lease.value == Resource("second")


@pytest.mark.anyio
async def test_generations_rejects_rotating_released_lease() -> None:
    async def finalize(_resource: Resource) -> None:
        pass

    async with Generations(lambda: Resource("current"), finalize) as generations:
        async with generations.acquire() as lease:
            pass

        with pytest.raises(RuntimeError, match="active"):
            await generations.rotate(lease)


@pytest.mark.anyio
async def test_generations_reports_failed_finalization_to_later_closers() -> None:
    async def finalize(_resource: Resource) -> None:
        raise RuntimeError("unable to finalize")

    generations = Generations(lambda: Resource("current"), finalize)

    with pytest.raises(RuntimeError, match="unable to finalize"):
        await generations.aclose()
    with anyio.fail_after(1):
        with pytest.raises(RuntimeError, match="unable to finalize"):
            await generations.aclose()
