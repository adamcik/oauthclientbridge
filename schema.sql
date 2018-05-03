pragma journal_mode=WAL;

drop table if exists tokens;
create table tokens(
  client_id text primary key,
  token blob
);

drop table if exists buckets;
create table buckets (
  key text primary key,
  value real not null default 0,
  updated int not null
);
