pragma journal_mode=WAL;

create table if not exists tokens(
  client_id text primary key,
  token blob,
  created_at integer
);
-- TODO: Consider WITHOUT ROWID;?
