-- BlueX Dental Planlama — bulud verilənlər bazası şeması
-- Supabase projekti yaradıldıqdan sonra: sol menyudan "SQL Editor" > "New query" >
-- bu faylın hamısını yapışdır > "Run".

create table if not exists public.hastalar (
  hasta_id   text primary key,
  owner      uuid references auth.users not null default auth.uid(),
  data       jsonb not null,
  updated_at timestamptz not null default now()
);

alter table public.hastalar enable row level security;

create policy "kendi hastalarini gorebilir"
  on public.hastalar for select
  using (auth.uid() = owner);

create policy "kendi hastalarini ekleyebilir"
  on public.hastalar for insert
  with check (auth.uid() = owner);

create policy "kendi hastalarini guncelleyebilir"
  on public.hastalar for update
  using (auth.uid() = owner);

create policy "kendi hastalarini silebilir"
  on public.hastalar for delete
  using (auth.uid() = owner);
