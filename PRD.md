# PRD: Social Media Comment Scraper (Facebook & Instagram)

## 1. Ringkasan Produk

Aplikasi CLI berbasis Python untuk mengambil komentar dari postingan Facebook dan Instagram yang **bersifat publik**, menggunakan Selenium untuk otomasi browser. Aplikasi menerima daftar URL dari file teks, memproses satu platform per eksekusi, dan menyimpan hasil scraping dalam format JSON.

### 1.1 Catatan Legal & Etika (WAJIB DIBACA SEBELUM IMPLEMENTASI)

- Scraping Facebook dan Instagram **melanggar Terms of Service** kedua platform tersebut (Meta Platform Terms & Community Standards melarang automated data collection tanpa izin resmi/API).
- Risiko yang harus disadari pengguna: pemblokiran IP, flagging akun, potensi tuntutan hukum (referensi kasus hiQ Labs vs LinkedIn dan kasus-kasus serupa menunjukkan area ini masih abu-abu secara hukum tergantung yurisdiksi).
- Alternatif yang lebih aman dan sesuai ToS: **Meta Graph API** / **Instagram Graph API** (memerlukan App Review dan izin dari pemilik akun/halaman).
- Aplikasi ini dibangun dengan asumsi penggunaan untuk riset internal, skala kecil, dan hanya pada konten yang sudah publik — bukan untuk scraping massal/komersial.
- Dokumen ini hanya mencakup teknik otomasi browser standar (kontrol Selenium, delay, randomisasi interaksi). Tidak mencakup teknik pelanggaran keamanan (bypass login, credential stuffing, captcha solving otomatis, dsb.) — hal-hal tersebut **di luar cakupan proyek ini dan tidak boleh diimplementasikan**.

---

## 2. Tujuan (Goals)

1. Mengambil komentar dari postingan publik FB & IG berdasarkan daftar URL input.
2. Meniru perilaku browsing manusia agar tidak mudah terdeteksi sebagai bot (dalam batas wajar — bukan bypass sistem keamanan).
3. Otomatis membatalkan proses jika terdeteksi bahwa konten memerlukan login (private/restricted).
4. Menyimpan hasil dalam format JSON terstruktur dengan penamaan file standar.
5. Memproses satu platform per eksekusi (tidak dicampur dalam satu run).

## 3. Non-Goals (Di Luar Cakupan)

- Login otomatis ke akun FB/IG.
- Scraping konten privat, grup tertutup, atau story.
- Bypass CAPTCHA otomatis.
- Scraping paralel/multi-thread skala besar.
- Scraping data selain komentar (like, share, profile info, dsb.) — kecuali metadata dasar yang disebut di §7.

---

## 4. User Flow

```
1. User menyiapkan file input .txt berisi daftar URL (1 URL per baris).
2. User menjalankan CLI dengan parameter platform (fb/ig) dan path file input.
3. Aplikasi membuka browser (mode "browser testing" / non-headless, terlihat GUI-nya).
4. Untuk setiap URL:
   a. Buka URL.
   b. Cek apakah halaman meminta login / redirect ke login wall.
      - Jika YA -> skip URL ini, catat status "skipped_login_required", lanjut ke URL berikutnya.
      - Jika TIDAK -> lanjutkan.
   c. Simulasikan perilaku scrolling & mouse movement natural.
   d. Load semua komentar (klik "Lihat komentar lainnya" / scroll pagination) dengan delay acak.
   e. Ekstrak data komentar.
   f. Simpan ke file JSON dengan format penamaan yang ditentukan.
5. Tampilkan ringkasan hasil (jumlah sukses, skip, gagal) di akhir proses.
```

---

## 5. Functional Requirements

### FR-1: Input Handling
- Input berupa file `.txt`, 1 URL per baris.
- Validasi setiap baris:
  - URL valid dan sesuai domain platform yang dipilih (`facebook.com`/`fb.watch` untuk FB, `instagram.com` untuk IG).
  - Baris kosong atau komentar (`#...`) diabaikan.
- Command line menerima parameter platform secara eksplisit (lihat §9), sehingga satu proses run hanya menangani satu platform.

### FR-2: Browser Automation
- Menggunakan Selenium WebDriver.
- Browser dijalankan dalam mode **"browser testing"**, yaitu:
  - Mode non-headless (GUI browser terlihat, bukan headless mode) agar perilaku rendering lebih natural dan mengurangi sinyal deteksi headless.
  - Gunakan Chrome/Chromium dengan `webdriver-manager` untuk auto-manage driver version.
- Window size di-randomize dalam rentang wajar (misal 1280x800 s.d. 1920x1080) agar tidak selalu identik antar sesi.
- User-Agent menggunakan UA browser umum yang valid dan konsisten dengan window size yang dipilih (hindari UA yang mencolok/tidak lazim).

### FR-3: Simulasi Perilaku Manusia
- **Gerakan mouse**: gunakan `ActionChains` untuk memindahkan kursor secara bertahap (multiple intermediate points, bukan langsung lompat ke elemen target), dengan kecepatan dan jalur yang sedikit acak (menggunakan sedikit noise pada koordinat, bukan garis lurus sempurna).
- **Scrolling**: scroll dilakukan bertahap dengan jarak piksel acak (misal 200–600px per step), bukan langsung scroll ke bawah penuh.
- **Delay/jeda**: setiap aksi (klik, scroll, buka URL baru) diberi jeda acak (misal 2–6 detik), gunakan distribusi acak (bukan interval tetap) agar pola tidak terlalu seragam.
- **Perilaku "melihat-lihat"**: sesekali browser melakukan scroll kecil ke atas lalu ke bawah lagi (simulasi orang membaca ulang) sebelum melanjutkan ke aksi berikutnya.
- Semua parameter delay/jeda di atas dapat dikonfigurasi lewat file config (lihat §10), dengan nilai default yang aman (tidak terlalu cepat).

### FR-4: Deteksi Login-Wall / Private Content
- Sebelum mulai ekstraksi komentar, aplikasi memeriksa indikator bahwa halaman meminta login, contoh:
  - URL redirect ke halaman `login.php` (FB) atau `/accounts/login/` (IG).
  - Munculnya modal/dialog "Log in to see more" atau elemen dengan pola serupa.
  - Konten utama (komentar/post) tidak termuat sama sekali setelah waktu tunggu maksimum.
- Jika salah satu indikator terdeteksi:
  - Hentikan proses untuk URL tersebut.
  - Catat status `"skipped_login_required"` beserta alasan di log dan di ringkasan akhir.
  - **Tidak boleh** mencoba metode apapun untuk melewati/bypass login wall tersebut.

### FR-5: Ekstraksi Komentar
- Load semua komentar yang tersedia secara publik dengan cara:
  - Klik tombol "Lihat X komentar lainnya" / "View more comments" berulang kali sampai tidak ada lagi, ATAU
  - Scroll pada container komentar (untuk Instagram) sampai tidak ada komentar baru yang termuat.
- Batas aman: gunakan `max_comments` (dari config) untuk membatasi jumlah komentar yang diambil per postingan agar proses tidak berjalan tanpa henti pada postingan viral.
- Field yang diekstrak per komentar (lihat §7 untuk skema lengkap).

### FR-6: Output
- Setiap postingan menghasilkan satu file JSON terpisah.
- Format nama file:
  ```
  <id_post>-<PLATFORM>-<ddmmyyhs>.json
  ```
  Contoh: `1234567890-FB-04086h.json` atau `Cabc123XyZ-IG-04089h.json`
  - `id_post`: ID unik postingan diekstrak dari URL.
  - `PLATFORM`: `FB` atau `IG`.
  - `ddmmyy`: tanggal (hari-bulan-tahun, 2 digit tahun).
  - `hs`: singkatan **h**our-**s**econd → gunakan format `HHMMSS` (jam-menit-detik) agar unik; sesuaikan penulisan sesuai preferensi tim, contoh akhir: `04080625-FB-153045.json` (perlu dikonfirmasi format waktu presisi ke user — lihat §12 Open Questions).
- Semua file output disimpan di folder `output/<platform>/`.

### FR-7: Logging & Report
- Log proses ke console dan file log (`logs/scrape_<timestamp>.log`), mencatat:
  - URL diproses, status (sukses/skip/gagal), jumlah komentar diambil, waktu proses.
- Di akhir eksekusi, tampilkan ringkasan:
  ```
  Total URL     : 10
  Sukses        : 7
  Skip (login)  : 2
  Gagal (error) : 1
  ```

### FR-8: Error Handling & Retry
- Jika terjadi error saat load halaman (timeout, elemen tidak ditemukan, dsb.):
  - Retry maksimal N kali (default 2) dengan delay antar retry.
  - Jika tetap gagal, catat status `"failed"` dan lanjut ke URL berikutnya (tidak menghentikan seluruh batch).
- Jika terdeteksi rate limiting / blokir sementara dari platform (misal muncul halaman "You're doing this too often" atau HTTP 429 pattern di UI):
  - Hentikan seluruh proses batch untuk platform tersebut, beri jeda cooldown, dan beri notifikasi ke user bahwa proses dihentikan agar akun/IP tidak diblokir lebih lanjut.

---

## 6. Non-Functional Requirements

| Aspek | Requirement |
|---|---|
| Bahasa & Runtime | Python 3.10+ |
| Browser | Chrome/Chromium terbaru, non-headless |
| Portability | Harus bisa dijalankan di Windows & Linux |
| Kecepatan | Diprioritaskan pada "natural pacing", bukan kecepatan maksimum |
| Konfigurasi | Semua parameter delay, retry, max_comments dapat diubah lewat file config tanpa mengubah kode |
| Reliability | Kegagalan satu URL tidak boleh menghentikan seluruh batch (kecuali kondisi rate-limit di FR-8) |
| Maintainability | Kode modular: terpisah antara `fb_scraper.py`, `ig_scraper.py`, `browser_utils.py`, `human_behavior.py`, `io_utils.py` |

---

## 7. Skema Data Output (JSON)

```json
{
  "post_id": "1234567890",
  "platform": "FB",
  "post_url": "https://www.facebook.com/.../posts/1234567890",
  "scraped_at": "2026-08-04T15:30:45+07:00",
  "post_caption": "Teks caption/isi postingan (jika publik dan tersedia)",
  "total_comments_scraped": 152,
  "comments": [
    {
      "comment_id": "abc123",
      "author_name": "Nama Publik Akun",
      "author_profile_url": "https://www.facebook.com/username",
      "comment_text": "Isi komentar",
      "comment_time": "2026-08-01T10:00:00+07:00",
      "like_count": 12,
      "reply_count": 3,
      "is_reply": false,
      "parent_comment_id": null
    }
  ]
}
```

Catatan:
- Field yang tidak tersedia/tidak bisa diekstrak diisi `null`, bukan dihapus dari skema (agar konsisten antar file).
- `comment_time` disimpan dalam format ISO 8601 jika platform menampilkan waktu absolut; jika platform hanya menampilkan relatif ("2j", "3 hari"), simpan juga raw text-nya di field tambahan `comment_time_raw`.

---

## 8. Arsitektur & Struktur Folder

```
comment-scraper/
├── main.py                  # entry point CLI
├── config.yaml               # semua parameter konfigurasi
├── requirements.txt
├── input/
│   ├── fb_urls.txt
│   └── ig_urls.txt
├── output/
│   ├── fb/
│   └── ig/
├── logs/
├── src/
│   ├── __init__.py
│   ├── browser_utils.py     # setup driver, opsi browser, UA, window size
│   ├── human_behavior.py    # simulasi mouse movement, scroll, delay acak
│   ├── login_wall_detector.py
│   ├── fb_scraper.py
│   ├── ig_scraper.py
│   ├── io_utils.py          # baca input, tulis JSON, penamaan file
│   └── logger.py
└── PRD.md
```

---

## 9. CLI Interface

```bash
python main.py --platform fb --input input/fb_urls.txt
python main.py --platform ig --input input/ig_urls.txt
```

Parameter:
| Argument | Wajib | Deskripsi |
|---|---|---|
| `--platform` | Ya | `fb` atau `ig`. Menentukan scraper mana yang dijalankan (tidak boleh campur). |
| `--input` | Ya | Path ke file `.txt` berisi daftar URL. |
| `--output-dir` | Tidak | Override folder output default. |
| `--max-comments` | Tidak | Override batas jumlah komentar per postingan dari config. |
| `--headful/--headless` | Tidak | Default headful (browser testing/terlihat) sesuai requirement; opsi headless disediakan untuk debugging saja dan tidak direkomendasikan karena meningkatkan risiko deteksi. |

---

## 10. Konfigurasi (`config.yaml`)

```yaml
browser:
  window_size_options:
    - [1366, 768]
    - [1440, 900]
    - [1920, 1080]
  headless: false

delays:
  between_actions_sec: [2, 6]
  between_urls_sec: [8, 20]
  scroll_step_px: [200, 600]

scraping:
  max_comments_per_post: 500
  max_retry: 2
  retry_delay_sec: 5

output:
  base_dir: "output"
  filename_time_format: "%d%m%y%H%M%S"
```

---

## 11. Alur Teknis Deteksi & Anti-Blokir (Ringkasan Pendekatan)

Pendekatan yang digunakan **murni bersifat "menjaga kewajaran pola akses"**, bukan menembus sistem keamanan:

1. Non-headless browser (mode browser testing) — mengurangi sinyal otomatis "headless".
2. Delay acak antar aksi & antar URL — meniru ritme baca manusia.
3. Gerakan mouse bertahap sebelum klik, bukan klik instan di koordinat.
4. Variasi window size antar sesi.
5. Cooldown otomatis + penghentian batch bila muncul sinyal rate-limit dari platform (lihat FR-8).
6. Tidak menjalankan request paralel/simultan ke platform yang sama.

**Yang secara sengaja TIDAK dilakukan** (di luar scope, berisiko tinggi secara hukum/etika): rotasi proxy/IP untuk menyamarkan identitas, spoofing fingerprint browser tingkat lanjut, automasi login, automated CAPTCHA solving.

---

## 12. Open Questions (perlu dikonfirmasi sebelum development)

1. Format persis `<ddmmyyhs>` pada nama file — apakah `hs` dimaksud sebagai "jam-detik" (`HHSS`) atau representasi lain? Draft ini mengasumsikan `HHMMSS` (jam-menit-detik) untuk keunikan nama file. Mohon konfirmasi format yang diinginkan.
2. Apakah caption/isi postingan wajib ikut disimpan, atau hanya komentar saja?
3. Apakah reply/balasan komentar perlu di-nest atau cukup flat list dengan `parent_comment_id`?
4. Berapa target skala harian (jumlah URL per hari) — ini akan memengaruhi rekomendasi nilai delay default agar risiko pemblokiran tetap rendah?

---

## 13. Rencana Pengujian (Testing Plan)

| Skenario | Ekspektasi |
|---|---|
| URL FB publik valid | Komentar berhasil diekstrak & file JSON tersimpan sesuai format nama |
| URL IG publik valid | Sama seperti di atas |
| URL FB private/butuh login | Status `skipped_login_required`, tidak ada file JSON dibuat |
| URL tidak valid (404/salah domain) | Status `failed`, dicatat di log, batch lanjut |
| File input berisi campuran domain FB & IG saat `--platform fb` | URL non-FB di-skip dengan warning, tidak diproses |
| Rate-limit/blokir sementara terdeteksi di tengah batch | Batch dihentikan, cooldown, notifikasi ke user |

---

## 14. Dependencies (requirements.txt draft)

```
selenium>=4.20.0
webdriver-manager>=4.0.0
pyyaml>=6.0
python-dateutil>=2.9.0
```

---

## 15. Ringkasan untuk AI Code Agent (Opencode)

Saat mengimplementasikan dari PRD ini, urutan kerja yang disarankan:
1. Setup struktur folder & `config.yaml` (§8, §10).
2. Implementasi `browser_utils.py` (setup driver non-headless, window size random, UA).
3. Implementasi `human_behavior.py` (mouse movement bertahap, scroll acak, delay acak) — modul ini dipakai bersama oleh FB & IG scraper.
4. Implementasi `login_wall_detector.py` (§FR-4) sebagai modul terpisah agar mudah di-unit-test.
5. Implementasi `io_utils.py` (baca file input, validasi URL per platform, penamaan & penulisan file JSON sesuai §FR-6).
6. Implementasi `fb_scraper.py` lalu `ig_scraper.py` (masing-masing memakai modul di atas).
7. Implementasi `main.py` sebagai CLI orchestrator (§9) dengan argument parsing, logging (§FR-7), dan summary report.
8. Tambahkan error handling & retry (§FR-8) di level orchestrator, bukan di masing-masing scraper, agar konsisten.
9. Tulis test manual sesuai §13 sebelum dipakai untuk data riil.

**Prioritas implementasi awal (MVP)**: FR-1, FR-2, FR-4, FR-5, FR-6 untuk satu platform dulu (disarankan Instagram karena struktur DOM komentar relatif lebih sederhana), baru lanjut ke Facebook dan fitur retry/rate-limit handling.
