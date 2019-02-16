pragma journal_mode=WAL;

drop table if exists tokens;
create table tokens(
  client_id text primary key,
  token blob
);
-- TODO: Consider WITHOUT ROWID;?
