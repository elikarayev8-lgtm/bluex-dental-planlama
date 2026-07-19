-- BlueX Dental Planlama — otomatik güncelleme kontrolü şeması
-- Supabase SQL Editor'de bu dosyanın tamamını çalıştırın (tek seferlik kurulum).

create table if not exists public.app_surumler (
  id           uuid primary key default gen_random_uuid(),
  surum        text not null,           -- örn. "1.1.0"
  indirme_url  text not null,           -- BlueXDental_Setup.exe'nin herkese açık indirme adresi
  notlar       text,                    -- kullanıcıya gösterilecek kısa değişiklik notu (opsiyonel)
  created_at   timestamptz not null default now()
);

alter table public.app_surumler enable row level security;

-- Herkes (giriş yapmamış kullanıcılar dahil) en son sürüm bilgisini okuyabilsin —
-- hasta verisi içermez, güvenlik riski yok. Ekleme/güncelleme/silme yalnız
-- Supabase panelinden (dashboard) veya service_role ile yapılır, anon key ile değil.
create policy "surumleri herkes okuyabilir"
  on public.app_surumler for select
  using (true);

-- Yeni bir sürüm yayınlarken (örnek):
-- insert into public.app_surumler (surum, indirme_url, notlar) values
--   ('1.2.0', 'https://<storage-url>/BlueXDental_Setup.exe', 'Diş yüzeyi işaretleme eklendi.');
