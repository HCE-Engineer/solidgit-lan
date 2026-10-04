# SolidGit LAN — Mimari Taslak (v0, tartışmaya açık)

**Tarih:** 2026-08-08

> **Not:** Bu, projenin başında yazılan tasarım taslağıdır ve kararların nasıl alındığını
> göstermek için olduğu gibi bırakılmıştır. Çoğu uygulandı, bazıları yolda değişti (örneğin
> kilitlere "yumuşak talep" eklendi, geçmiş HEAD/TIP olarak ikiye ayrıldı, çakışmasız ayrılıklar
> kendiliğinden birleşiyor). Güncel durum için [README](../README.md).

---

## 0. Senaryo

1. Ben montajımı uygulamaya atarım.
2. Bilgisayarımın hotspot'unu açarım → yerel ağ oluşur.
3. Arkadaşlarım o ağa bağlanır, uygulama onları görür.
4. Kimde daha yeni sürüm varsa (iki tarafın da onayıyla) o sürüm alınır ve herkese dağıtılır.
5. Artık herkeste aynı dosya seti var.
6. Biri bir parça üzerinde çalışırken o parça kilitlenir.
7. Gerisi "bildiğin git" — commit, geçmiş, geri alma.

---

## 1. En kritik tespit: Bu aslında Git değil, PDM/Perforce

Git'in dağıtık olmasının sebebi **metin dosyalarının birleştirilebilmesidir** (merge).
`.sldprt` / `.sldasm` ikili (binary) dosyalar — iki kişinin aynı parçadaki değişikliği
**hiçbir algoritma birleştiremez**. Dolayısıyla:

| Git'te var | Bizde |
|---|---|
| Branch + merge | ❌ Gerek yok (birleştirilemez) — tek doğrusal ana hat |
| 3-way merge | ❌ İmkânsız |
| Herkes eşit yetkili | ⚠️ Kilit için **tek hakem** lazım |
| Çakışmayı sonradan çöz | ✅ Çakışmayı **önceden engelle** (kilit) |
| Satır bazlı geçmiş | ✅ Dosya bazlı snapshot geçmişi |

Yani hedefimiz: **"Git gibi hissettiren, içeride Perforce/SolidWorks PDM gibi çalışan"** bir sistem.
Kullanıcıya `commit / geçmiş / geri al / kim ne yaptı` gösteririz; altta ise
*kilitli tek-yazar + doğrusal snapshot zinciri* vardır. Bu, hem çok daha basit hem de
CAD için **doğru** olan model.

---

## 2. Topoloji: Yıldız + devredilebilir koordinatör

Üç seçenek vardı:

- **A. Saf P2P mesh** — herkes herkese eşit. Kilit için dağıtık konsensüs (Raft/CRDT) gerekir.
  Bir dönem projesi değil, bir doktora tezi. ❌
- **B. Sabit sunucu** — hep aynı makine sunucu. Hotspot sahibi kapatınca her şey durur. ❌
- **C. Yıldız + seçim (election)** ✅ **ÖNERİ**

**C nasıl çalışır:**
- Hotspot'u açan kişi doğal olarak **Koordinatör (Host)** olur. Kilit tablosunun ve resmî
  commit zincirinin *hakemi* odur.
- Diğerleri **Peer**'dır — ama herkeste **tam kopya** vardır (yedek garantisi, git'in tek
  gerçek avantajı burada korunur).
- Koordinatör düşerse (laptop kapandı, ağdan çıktı): kalanlar arasında **yeniden seçim**.
  En uzun commit zincirine sahip olan aday olur, kullanıcı onaylar.
- Koordinatör rolü elle de devredilebilir: "Ben çıkıyorum, Ahmet host olsun."

> Kilit (lock) kavramı doğası gereği **tek bir hakem** ister. Bu yüzden mimarinin
> merkezinde koordinatör var. Ama veri dağıtık — kimse veriyi rehin alamaz.

---

## 3. Katman mimarisi (klasör yapısı)

Ağ + COM + UI aynı yerde karışırsa test edilemez hale gelir. Sert sınırlar:

```
solidgit_lan/
  core/                 # SAF MANTIK — ağ yok, COM yok, UI yok. %100 unit-test edilebilir.
    objects.py          #   içerik-adresli depo (sha256 → blob)
    commits.py          #   commit zinciri, ata/ardıl hesabı, divergence tespiti
    manifest.py         #   çalışma alanı durumu (path → hash, mode, mtime)
    locks.py            #   kilit tablosu, kiralama (lease) mantığı
    reconcile.py        #   iki farklı geçmişi karşılaştır → uzlaşma planı üret
    ignore.py           #   ~$ dosyaları, .bak, geçici dosya kuralları
  net/                  # AĞ — core'u kullanır, core net'i bilmez
    discovery.py        #   mDNS + UDP broadcast ile peer bulma
    server.py           #   koordinatörün HTTP/WS sunucusu
    client.py           #   peer'ın istemcisi
    protocol.py         #   mesaj şemaları + sürüm numarası
    security.py         #   katılım kodu, anahtar türetme, TLS parmak izi
    transfer.py         #   parçalı/devam ettirilebilir dosya aktarımı
  sw/                   # SOLIDWORKS — mevcut projeden BÜYÜK ÖLÇÜDE TAŞINABİLİR
    service.py          #   (mevcut sw_service.py)
    monitor.py          #   (mevcut sw_monitor.py)
    top_finder.py       #   (mevcut sw_top_finder.py)
    docmgr.py           #   YENİ: Document Manager API — önizleme + özellik okuma
    fake.py             #   YENİ: SolidWorks'süz test için sahte sağlayıcı
  ui/                   # ARAYÜZ — sadece core+net'in olaylarını dinler
  app.py                # kompozisyon kökü (dependency wiring)
```

**Kural:** `core/` hiçbir şey import etmez (stdlib hariç). Bu sayede ağ ve SolidWorks olmadan
tüm mantık test edilebilir. Bu, projenin test edilebilirliğini belirleyen tek en önemli karar.

---

## 4. Veri modeli

### 4.1 Neden git/git-lfs kullanmıyoruz?

- Kurulum yükü: her arkadaşın makinesine git + git-lfs kurdurmak = UX cehennemi.
  Bizim hedefimiz "exe'yi çalıştır, ağa bağlan, bitti."
- Git'in delta sıkıştırması ikili CAD dosyalarında neredeyse hiç kazanç sağlamaz.
- `.git` klasörü 200MB'lık montajlarda şişer, `gc` işkenceye döner.
- Kilit ve dağıtım mantığını zaten kendimiz yazıyoruz.

### 4.2 Kendi depomuz (basit ve yeterli)

```
<workspace>/
  Montaj1/                      ← KULLANICININ ÇALIŞTIĞI GERÇEK DOSYALAR
    govde.sldprt
    kapak.sldprt
    montaj.sldasm
  .solidgit/                    ← BİZİM DEPO (kullanıcı dokunmaz)
    repo.json                   ← repo_id, isim, protokol sürümü
    objects/                    ← içerik-adresli blob deposu
      a3/f9c2...zst             ← sha256'nın ilk 2 hanesi klasör, zstd sıkıştırılmış
    commits/
      000001.json ... 000342.json
    HEAD                        ← şu anki commit id
    locks.json                  ← kilit tablosu (koordinatörde otorite, peer'da kopya)
    peers.json                  ← tanınan cihazlar + anahtarları
    tmp/                        ← atomik yazma için (yaz → fsync → rename)
```

**Commit nesnesi:**
```json
{
  "id": "c000342",
  "parents": ["c000341"],
  "author": {"name": "Hüseyin", "device": "HUSEYIN-PC", "key_id": "…"},
  "lamport": 342,
  "wall_clock": "2026-08-08T20:15:00+03:00",
  "message": "Gövde kalınlığı 3mm'ye çıkarıldı",
  "files": {
    "Montaj1/govde.sldprt": {"sha256": "a3f9c2…", "size": 4821004},
    "Montaj1/montaj.sldasm": {"sha256": "9b1d0e…", "size": 12904112}
  }
}
```

Bu yapının bedava getirdikleri:
- **Dosya bazlı tekilleştirme (dedup):** 40 commit'te değişmeyen bir parça diskte **1 kez** durur.
- **Senkronizasyon aptalca basit:** "Bende bu 12 hash yok, gönder." Bitti.
- **Bütünlük:** her blob'un adı kendi hash'i. Bozulma anında yakalanır.
- **Çöp toplama (GC) bizde:** "son 20 commit + etiketlenenleri tut, gerisini sil."

### 4.3 Saat sorunu — "kimde daha yeni?" sorusunun doğru cevabı

⚠️ **Makine saatleri asla güvenilmez.** Arkadaşın saati 2 saat geride olabilir.
"Daha yeni" kararını **duvar saatiyle değil, commit zinciriyle** veririz:

- `B`'nin HEAD'i `A`'nın HEAD'inin **atası** mı? → `B` geride, ileri sarsın (fast-forward).
- `A`'nınki `B`'nin atası mı? → `A` ileri sarsın.
- İkisinde de diğerinde olmayan commit var mı? → **IRAKSAMA (divergence)** → Bölüm 6.

Duvar saatini sadece **kullanıcıya göstermek** için tutarız, karar için değil.

---

## 5. Ağ katmanı

### 5.1 Keşif (discovery)
- **Birincil:** mDNS/Zeroconf — `_solidgit._tcp.local.`
  Yayınlanan bilgi: cihaz adı, kullanıcı adı, repo_id, protokol sürümü, HEAD, rol (host/peer).
- **Yedek:** her 2 saniyede UDP broadcast (bazı hotspot sürücüleri multicast'i boğar).
- **Son çare:** elle IP girme + QR kod / 6 haneli kod.

### 5.2 Taşıma (transport)
**Öneri: HTTP + WebSocket (FastAPI/uvicorn).** Neden:
- Büyük dosya aktarımı için `Range` header'ı ile **kaldığı yerden devam** bedava gelir.
- WebSocket ile **canlı olaylar**: "Ahmet kapak.sldprt'yi kilitledi" anında herkese düşer
  (polling yok).
- Tarayıcıdan debug edilebilir, `curl` ile test edilebilir.
- Ham TCP + msgpack daha hızlı olurdu ama bu ölçekte kazanç yok, iş yükü çok.

### 5.3 Güvenlik (hotspot = "yarı güvenli" ortam)
Hotspot'un WPA2 şifresi var ama ağa giren herkes servisi görür. Katmanlar:

1. **Katılım kodu:** Host ekranda gösterir → `SG-4F2A-9C1B`. Bundan scrypt ile anahtar türetilir.
2. **TLS (kendi imzalı sertifika)** + sertifika parmak izinin ilk hanelerini katılım koduna gömme
   → ortadaki-adam saldırısı kapanır.
3. **Cihaz onayı:** Kod doğru olsa bile host ekranında çıkar:
   *"AHMET-LAPTOP (192.168.137.42) katılmak istiyor — İzin Ver / Reddet / Hep izin ver."*
4. **Rol/yetki:** salt-okunur izleyici / katkıcı / yönetici.

### 5.4 Windows'a özgü tuzaklar (şimdiden bilelim)
- **Güvenlik Duvarı** gelen bağlantıyı sessizce keser. Uygulama bunu tespit edip kural eklemeyi
  önermeli (yönetici izni ister) — yoksa "arkadaşlarım beni göremiyor" 1 numaralı şikâyet olur.
- **Mobil Erişim Noktası en fazla 8 cihaz** destekler.
- Hotspot ağı `192.168.137.x` sabit aralığındadır — teşhis için faydalı.
- Hotspot'ta radyo paylaşılır: 200MB'ı 4 kişiye göndermek yavaştır → Bölüm 8'deki "sürü (swarm)"
  fikri.

---

## 6. İlk buluşma / iraksama uzlaştırması (senin özellikle vurguladığın kısım)

Bu, projenin **en riskli ve en değerli** parçası. Kural: **asla otomatik üzerine yazma.**

**Akış:**
1. İki peer el sıkışır, commit id listelerini değişir (küçük veri, hızlı).
2. `core/reconcile.py` bir **Uzlaşma Planı** üretir:
   - Sadece bende olan dosyalar
   - Sadece onda olan dosyalar
   - İkisinde de olup **farklı** olanlar ← asıl karar noktası
3. UI bunu yan yana gösterir: dosya adı, boyut, son değiştiren, tarih, **gömülü önizleme resmi**,
   varsa parça numarası/revizyon özelliği.
4. Kullanıcı seçer: "Taban benimki olsun + onun şu 3 dosyasını al" veya dosya bazında tek tek.
5. **Çift onay:** Plan karşı tarafa gönderilir, o da *aynı planı* görüp onaylar. İki taraf da
   "Evet" demeden hiçbir bayt yazılmaz.
6. Sonuç: iki ebeveynli bir **uzlaşma commit'i**. Kaybedilen sürüm silinmez — depoda kalır,
   geçmişten geri alınabilir.

**Özel durumlar:**
- Farklı `repo_id` (alakasız iki proje) → reddet, "ayrı proje olarak içe aktar" öner.
- Sonradan katılan (15:00'te gelen) → aynı akış, tek fark: muhtemelen sadece ileri sarma.
- Kimse birbirini tanımıyor, ilk kurulum → biri "projeyi ben başlatıyorum" der, diğerleri klonlar.

---

## 7. Kilit modeli

- **Granülerlik:** dosya başına (parça/montaj/teknik resim).
- **Kiralama (lease):** kilit süreye bağlıdır (örn. 4 saat), uygulama açıkken otomatik yenilenir.
  Laptop ölürse kilit kendiliğinden düşer → "Ahmet tatile gitti, dosya sonsuza dek kilitli" olmaz.
- **Zorlama (enforcement):** kilitli olmayan dosyalar diskte **salt-okunur** yapılır (Windows
  öznitelik). SolidWorks o dosyayı salt-okunur açar. Bu tavsiye değil, gerçek zorlamadır.
- **Otomatik kilit:** `sw_monitor` zaten açık belgeleri izliyor → kullanıcı bir parçayı düzenlemek
  için açtığı anda kilit istenir. **Bu kod bizde zaten var ve canlı test edilmiş.**
- **Montaj kaskadı:** Bir montajı kilitlemek alt parçaları etkiler. `sw_top_finder`'ın bağımlılık
  grafiği ile: *"Bu montaj 14 parçaya bağlı — sadece montaj dosyasını mı, tüm ağacı mı?"*
- **Kilitsiz commit yasak** (yönetici uyarısıyla zorla geçilebilir, log'a yazılır).
- **Zorla açma:** yöneticide "Ahmet'in kilidini kır" var, ama Ahmet'e bildirim gider.

---

## 8. Özellik listesi

### 🔴 MVP — bu olmadan uygulama yok
| # | Özellik |
|---|---|
| 1 | Çalışma alanı oluştur / var olanı içe aktar (klasörü ilk commit yap) |
| 2 | Otomatik peer keşfi (mDNS + broadcast), canlı peer listesi |
| 3 | Katılım kodu + host onayı ile ağa katılma |
| 4 | İlk senkron: tam kopya indirme (ilerleme çubuğu, devam ettirilebilir) |
| 5 | İraksama uzlaştırma ekranı + **çift onay** |
| 6 | Dosya kilitle / bırak, kilit tablosu canlı görünüm |
| 7 | Kilitsiz dosyaları salt-okunur yapma (gerçek zorlama) |
| 8 | Commit (mesajla) + herkese yayma |
| 9 | Geçmiş görünümü: kim, ne zaman, hangi dosyalar, mesaj |
| 10 | Geri alma: bir dosyayı/tüm commit'i eski sürüme döndür |
| 11 | Bağlantı kopma dayanıklılığı: yarım aktarım bozuk dosya bırakmaz |

### 🟡 v1 — "bunsuz olur ama kötü olur"
| # | Özellik |
|---|---|
| 12 | SolidWorks açıkken kaydedilmemiş değişiklik varsa senkronu engelle/uyar |
| 13 | Gömülü önizleme küçük resmi (dosyayı açmadan, Document Manager API) |
| 14 | Parça no / revizyon / malzeme / açıklama özelliklerini okuyup listede gösterme |
| 15 | Koordinatör devri + otomatik yeniden seçim (host çıkarsa) |
| 16 | Kilit kiralama + otomatik düşme + zorla açma (bildirimli) |
| 17 | Güvenlik duvarı kuralı sihirbazı + ağ teşhis paneli ("neden göremiyorum?") |
| 18 | Etkinlik akışı / bildirimler ("Ahmet kapak.sldprt'yi commit'ledi") |
| 19 | Yoksay kuralları (`~$*`, `*.bak`, geçici dosyalar) |
| 20 | Çöp toplama + saklama politikası ("son N sürümü tut") |
| 21 | Çevrimdışı çalışma → sonra bağlanınca uzlaştırma |
| 22 | Bağımlılık grafiği görünümü (montaj ağacı, `sw_top_finder` ile) |
| 23 | eDrawings ile aç (SolidWorks'ü olmayan takım arkadaşı için) |

### 🟢 v2 — cila ve güç özellikleri
| # | Özellik |
|---|---|
| 24 | **Sürü aktarımı (swarm):** parçayı alan peer, diğerine servis eder → hotspot'ta 3-4x hız |
| 25 | Parça bazlı (chunk) transfer — sadece değişen bloklar gider |
| 26 | Etiketler / sürüm damgaları ("Rev-B, müşteriye giden") |
| 27 | Görev/atama: "bu parça Ahmet'e atandı" |
| 28 | Toplu dışa aktarma (PDF/DXF/STEP) — eski projedeki Milestone 6 |
| 29 | BOM (malzeme listesi) → Excel — eski projedeki Milestone 7 |
| 30 | Görsel fark: iki sürümün önizleme resmini yan yana / kaplama |
| 31 | Denetim kaydı (audit log) — kim neyi zorla açtı, ne zaman |
| 32 | Bulut köprüsü: internet varken bir sunucuya/Drive'a yedek itme |
| 33 | Web arayüzü — sunucu zaten HTTP konuşuyor, host'un IP'sini tarayıcıdan açmak bedava gelir |
| 34 | Salt-okunur "gözlemci" modu (hoca/müşteri sadece izler) |
| 35 | Otomatik yedek: her commit'te bir dış diske/klasöre kopya |

### 💡 Çılgın ama düşünülmeye değer
- **Zaman tüneli:** geçmişte bir tarihe kaydırınca tüm çalışma klasörü o âna döner (Time Machine).
- **"Kim neyi bekliyor" paneli:** kilit kuyruğu — "Ahmet gövde'yi bıraktığında bana haber ver."
- **Ölçü/ağırlık takibi:** her commit'te kütle özelliklerini kaydet → "montaj 3 haftada 12kg
  arttı" grafiği.
- **Mobil izleyici:** telefondan hotspot'a bağlanıp host'un web sayfasından durumu görme.

---

## 9. Geliştirme süreci mimarisi

Bu bir **ağ uygulaması** — "çalıştır ve bak" ile geliştirilemez. Süreci baştan buna göre kurmalıyız.

### 9.1 Tek makinede 3 kopya çalıştırabilmek (EN ÖNEMLİ KARAR)
Daha ilk günden uygulama şu parametreleri almalı:
```
python -m solidgit_lan --workspace C:\test\peer_a --port 7801 --name "Peer A" --fake-sw
```
Böylece tek bilgisayarda 3 pencere açıp **gerçek ağ olmadan** tüm senaryoyu (katılma, iraksama,
kilit çakışması, host düşmesi) test edebiliriz. Bunu sonradan eklemek imkânsıza yakındır.

### 9.2 Sahte SolidWorks sağlayıcısı (`sw/fake.py`)
`sw/` katmanı bir arayüz (protocol) tanımlar; gerçek COM sürümü ve sahte sürümü aynı arayüzü
uygular. SolidWorks kurulu olmayan makinede/CI'da her şey test edilebilir.

### 9.3 Protokol önce, kod sonra
`docs/02-PROTOKOL.md` yazılacak ve **sürümlenecek** (`X-SolidGit-Protocol: 1`).
Sebep: takım arkadaşının makinesinde eski sürüm olacak. Uyumsuz sürüm gördüğünde uygulama
"güncelle" demeli, bozuk davranmamalı.

### 9.4 Dayanıklılık testi (chaos)
- Aktarımın ortasında kabloyu çek → yeniden bağlanınca kaldığı yerden devam, bozuk dosya yok.
- Host'u aniden kapat → seçim çalışıyor mu?
- Aynı dosyayı iki kişi aynı anda kilitlemeye çalışsın → biri kazanmalı, ikisi değil.
- Disk dolu senaryosu → temiz hata, yarım yazılmış dosya yok.
- **Atomik yazma zorunlu:** `tmp/` → fsync → rename. Asla hedefin üstüne doğrudan yazma.

### 9.5 Milestone planı (öneri)
| M | İçerik | Doğrulama |
|---|---|---|
| 0 | İskelet + çoklu-kopya çalıştırma + sahte SW | ✅ **TAMAM** — CLI `--workspace` ile aynı makinede N kopya |
| 1 | `core/` — obje deposu, commit, manifest, kilit, iraksama | ✅ **TAMAM** — 56 test geçti + uçtan uca iraksama senaryosu oynatıldı |
| 2 | Keşif + el sıkışma + peer listesi | ✅ **TAMAM** — 3 ayrı süreç birbirini buldu, sertifika sabitlendi, katılım onaya düştü |
| 3 | Tam senkron aktarım (devam ettirilebilir) | 500MB klasör aktarılıyor, kablo çekme testi |
| 4 | Kilit servisi + salt-okunur zorlama | iki kopya aynı kilidi isteyince biri reddediliyor |
| 5 | İraksama uzlaştırma + çift onay | ayrı ayrı çalışıp buluşma senaryosu |
| 6 | UI (dashboard, dosya listesi, geçmiş, peer paneli) | ekran görüntüsüyle |
| 7 | SolidWorks entegrasyonu (mevcut kod taşınır) | **gerçek SW ile canlı test** |
| 8 | **GERÇEK HOTSPOT TESTİ** — 2+ fiziksel makine | asıl sınav bu |

> M8'i sona bırakmıyoruz — M2 biter bitmez bir kez gerçek hotspot'ta denemeliyiz.
> "Windows güvenlik duvarı / mDNS boğulması" gibi sürprizler ancak orada çıkar.

---

## 10. Mevcut SolidGit projesinden ne kurtarılır?

| Dosya | Durum |
|---|---|
| `sw_service.py`, `sw_monitor.py`, `sw_top_finder.py` | ✅ **Neredeyse aynen taşınır** — canlı test edilmiş, en değerli varlık |
| `ui/widgets.py` (FileTable, StatusDot) | ✅ Taşınır |
| `ui/app.py` iş kuyruğu deseni (queue + after) | ✅ Desen taşınır, içerik yenilenir |
| `config.py` (tip doğrulamalı config) | ✅ Taşınır |
| `git_service.py`, `github_service.py`, `git_credential.py` | ❌ Artık gereksiz (git yok) |
| `lfs_lock_service.py` | ❌ Silinir (LFS kilidi yerine kendi kilidimiz) |
| `ui/dashboard_view.py`, `ui/file_manager_view.py` | ⚠️ Kavramlar değişti, yeniden yazılır |

Kabaca **%35-40'ı doğrudan yeniden kullanılabilir** — özellikle en zor kısım olan SolidWorks
COM entegrasyonu hazır.

---

## 11. Alınan kararlar (2026-08-08)

| # | Karar | Sonuç |
|---|---|---|
| 1 | **Arayüz: PySide6/Qt** | Tkinter kodu taşınmıyor. `ui/widgets.py` yeniden yazılır. Qt'nin sinyal/slot mekanizması, `ui/app.py`'deki elle yazılmış `queue + after(100)` desenini gereksiz kılar. |
| 2 | **Dağıtım: tek exe (PyInstaller)** | Baştan hedeflenir — arkadaşlar Python/git kurmaz. Mimari etkisi: kaynak dosya yolları `sys._MEIPASS` üzerinden çözülmeli, config/veri `%LOCALAPPDATA%`'ya yazılmalı, sertifika üretimi çalışma anında olmalı, imzasız exe için SmartScreen uyarısı beklenmeli. |
| 3 | **Ölçek: 3-4 kişi, ~200MB** | Sürü (swarm) aktarımı ve chunk transferi **v2'ye** ertelendi. MVP'de basit doğrudan aktarım. Ancak `net/transfer.py` bir arayüzün arkasına konur ki swarm sonradan eklenebilsin. |

| 4 | **Tamamen yerel — internet yok** | Bulut/uzak sunucu yok. Depo düz dosyalardan ibaret olduğu için "buluta yedekle" ileride sadece klasör kopyalamaktır; MVP'de hiçbir hazırlık gerekmiyor. |
| 5 | **Kilit birimi: dosya** (+ arayüzde grup kolaylığı) | Bkz. Bölüm 15. |
| 6 | **Ayrı proje: `SolidGit_Lan`** | `sw/` kodu mevcut SolidGit'ten kopyalanır. |

---

## 15. Kilit Granülerliği

Üç ihtimal vardı:

| Seçenek | Örnek | Karar |
|---|---|---|
| **Dosyadan ince** (feature bazlı) | Ben delikleri açarken sen aynı dosyanın kabartmasını düzenle | ❌ **İmkânsız** — SolidWorks dosyayı bütün olarak açar, bütün olarak kaydeder. Parçalı yazma yok. |
| **Dosya bazlı** | `govde.sldprt` bütünüyle bende | ✅ **Tek gerçek birim** — SolidWorks'ün gerçeği bu |
| **Gruptan kaba** (alt-montaj demeti) | "Şasi alt-montajı bende" → 1 montaj + 12 parça tek hamlede | ✅ Ama **ayrı bir kavram değil** |

**Karar:** Depo ve protokol katmanında birim **dosyadır**. Grup kilidi ayrı bir varlık değil,
sadece "aynı anda N dosya kilidi al" demektir → **arayüz kolaylığı, çekirdek karmaşası değil.**

### ⚠️ Bunun protokole getirdiği zorunluluk: atomiklik

Çoklu kilit isteği **hep ya da hiç** olmalıdır.

12 kilit istendi, 9'u verildi, 3'ü başkasındaysa → verilen 9'u da geri al.
Aksi halde klasik **kilitlenme (deadlock)**: bende 9, onda 3, ikimiz de ilerleyemeyiz ve
kimse neden takıldığını anlamaz. Kısmi başarı diye bir sonuç olmayacak.

---

## 12. Kapalı Klasör (Self-Contained Folder) İlkesi

**Karar:** Projeye bir **klasör** atılır. Gerekli tüm dökümanlar o klasörün içinde
olmak **zorundadır**. Klasör dışına referans **yasaktır**. Referans çözümü **göreli yol** ile
yapılır (herkeste klasör ağacı aynı, kök farklı olabilir).

Bu tek kural, mimaride birçok problemi aynı anda çözüyor:

### 12.1 Kapanış (closure) doğrulayıcı — `sw/closure.py`

İçe aktarma anında ve her commit öncesi:
1. Klasördeki her `.sldasm` / `.slddrw` için `GetDocumentDependencies2()` çalıştırılır
   (bu API **canlı test edilmiş**, `SldWorks.Application` üzerinde — `Extension`'da değil).
2. Çözülen yolu proje klasörünün **dışında** kalan her bağımlılık = **ihlal**.
3. Kullanıcıya ihlal listesi gösterilir, üç seçenek: **İçeri kopyala** / **Yoksay (kütüphane)** /
   **İptal**.

> Bu kural genel olduğu için **Toolbox ayrı bir problem olmaktan çıkıyor** — Toolbox parçası da
> sadece "klasör dışında duran bir bağımlılık"tır, aynı mekanizma yakalar.

### 12.2 Toolbox — üç strateji

| Strateji | Nasıl | Artı | Eksi |
|---|---|---|---|
| **A. Pack and Go ile içeri al** ⭐ | SolidWorks'ün kendi `IPackAndGo` API'si; `IncludeToolboxComponents = True` + tek klasöre düzleştir. Referansları SolidWorks kendisi yeniden yazar. | Native çözüm, referans yeniden yazma bedava, tek seferlik | Diğer makinede SolidWorks yerel Toolbox'a geri yönlenebilir → **2 makineli gerçek test şart** |
| **B. "Create Parts" modu** | SolidWorks Toolbox ayarında her donanım öğesi normal `.sldprt` olarak üretilir | En sağlam, geri yönlenme riski sıfır — artık Toolbox parçası değil, sıradan parça | Akıllı bağlantı elemanı davranışı kaybolur, kullanıcının SW ayarını değiştirmesi gerekir |
| **C. Yoksay + "sende de var" varsay** | Toolbox senkronlanmaz, herkesin kendi Toolbox'ı kullanılır | Sıfır iş | Sürüm/ayar farkı olan makinede **cıvata boyu sessizce değişir** — klasik SolidWorks felaketi. ❌ Kapalı klasör ilkesine de aykırı |

**Öneri: A, B'ye geçiş kapısı açık.** Önce Pack and Go denenir; 2 makineli testte geri yönlenme
görülürse B'ye düşülür.

**Boyut endişesi yok:** Bir cıvata ~100-300KB. 50 bağlantı elemanı ≈ 10-20MB, üstelik
içerik-adresli depoda **tekilleştirildiği için** commit'ler boyunca 1 kez saklanır.

### 12.3 Göreli referansın gizli tuzağı

SolidWorks bir montajı açarken **önce mutlak yolu** dener, tutmazsa göreli yola düşer.
Tehlike: mutlak yol karşı makinede *tesadüfen mevcutsa ama yanlış/eski dosyaya işaret ediyorsa*,
SolidWorks sessizce yanlış parçayı yükler ve kimse fark etmez.

**Önlem:** Çalışma alanı kök klasörünün adı `repo_id`'nin ilk hanelerini içerir
(örn. `Montaj1 [4f2a9c]`). Böylece iki farklı projenin yolu asla çakışmaz, mutlak yol
tesadüfen tutmaz, her zaman göreli çözüm devreye girer.

---

## 13. Kilit Yaşam Döngüsü (kesinleşti)

**Karar:** Orta çözüm + kilit kalkınca herkese bildirim.

### 13.1 Üç durum

```
  SERBEST ──(SolidWorks'te açıldı)──▶ YUMUŞAK TALEP ──(ilk değişiklik)──▶ KİLİTLİ
     ▲                                      │                              │
     │                                      │                              │
     └──────(kaydetmeden kapatıldı)─────────┴──────(commit + kapat)────────┘
```

1. **YUMUŞAK TALEP (soft claim):** Dosya SolidWorks'te açıldığı anda herkese duyurulur:
   *"Hüseyin gövde.sldprt'yi açtı (henüz düzenlemedi)."*
   Kilit değildir — kimseyi engellemez, sadece görünürlük sağlar.
2. **KİLİTLİ:** Belge ilk kez "kirli" (dirty) olduğunda gerçek kilit istenir.
   Kirlilik tespiti: `IModelDoc2.GetSaveFlag()` — dokümante API. (⚠️ canlı doğrulanacak.)
3. **SERBEST:** Kaydetmeden kapatılırsa kilit **sessizce** düşer, geçmişe hiçbir şey yazılmaz.

**Yumuşak talep neden var — kritik yarış durumu:**
Yumuşak talep olmasaydı: iki kişi aynı dosyayı açar, ikisi de 20 dakika çalışır, ikisi de kilit
ister, biri kaybeder ve **20 dakikalık emeği çöpe gider**. Yumuşak talep bunu daha ilk saniyede
görünür kılar: ikinci kişi dosyayı açar açmaz *"Bu dosyayı Hüseyin de açık tutuyor"* uyarısını
alır, emek harcamadan geri çekilebilir.

### 13.2 Bildirimler ve kilit kuyruğu

- Kilit kalkınca **herkese** bildirim: *"gövde.sldprt serbest bırakıldı (Hüseyin, 14:32)."*
- Kilit isteyip reddedilen kişi **kuyruğa** girebilir: *"Serbest kalınca bana haber ver."*
  Kuyruktakine daha güçlü bir bildirim gider: *"gövde.sldprt serbest — şimdi kilitle?"* (tek tık)
- Sıra garantisi: kuyruktaki ilk kişiye **30 saniyelik öncelik penceresi** verilir, o sürede
  başkası kapamaz. Yoksa "kim daha hızlı tıkladı" yarışı olur.

### 13.3 Kenar durumlar (devamı Bölüm 14'te)
- **SolidWorks çöktü / laptop kapandı:** kiralama (lease) süresi dolar, kilit otomatik düşer.
- **Ağ koptu ama kullanıcı çalışmaya devam etti:** kilit yerelde korunur; yeniden bağlanınca
  kilidin hâlâ geçerli olup olmadığı sınanır, değilse iraksama uzlaştırmasına düşer.
- **Zorla açma (yönetici):** sahibine ayrı ve belirgin bildirim gider, denetim kaydına yazılır.

---

## 14. Yayılım Modeli: "Çek" (pull) — kullanıcı hazır olunca

**Karar:** Commit anında kimseye zorla itilmez. Herkese **bildirim** gider, kişi
hazır olduğunda çeker.

### 14.1 Bu modelin tek gerçek tehlikesi ve çözümü

"Çek" modelinde insanlar **eski tabanla** çalışmaya devam eder. Asıl tehlike şu:
Ahmet `govde.sldprt`'yi güncelleyip commit etti; ben hâlâ eski sürümdeyim; ben de o dosyayı
kilitleyip düzenlersem, commit ettiğimde **Ahmet'in işini eziyorum.**

**Çözüm — tek ve basit bir kural:**

> ### 🔒 Bir dosyayı kilitleyebilmek için o dosyanın **güncel sürümüne sahip olmalısın.**

Kilit isteği geldiğinde koordinatör kontrol eder: istekçinin elindeki hash, o dosyanın en son
commit'teki hash'i mi? Değilse kilit reddedilir:

> *"gövde.sldprt'nin daha yeni bir sürümü var (Ahmet, 14:32). Kilitlemeden önce güncellemelisin."*
> **[Bu dosyayı güncelle ve kilitle]**  [Vazgeç]

Bu tek kural, "çek" modelini **güvenli** hale getirir:
- Güncellemek zorunda olduğun dosya, zaten *birazdan açacağın* dosyadır → SolidWorks'te açık
  değildir → altından çekilme sorunu yaşanmaz.
- Güncelleme **tek dosyalık ve hedeflidir** — 200MB'lık tam senkron beklemezsin.
- Kilidi olmayan dosyayı commit edemediğin için, **hiçbir yol sessiz üzerine yazmaya çıkmaz.**

### 14.2 Montaj bayatlığı (assembly staleness)

Parçalar için yukarıdaki kural yeter, ama montajlarda ek bir incelik var: `montaj.sldasm`'i
kilitleyip düzenlerken alt parçalardan biri bayatsa, montaj **eski geometriyle** yeniden
oluşturulur — hata anında değil, günler sonra "mate hatası" olarak patlar.

**Önlem:** Bir montaj kilitlenirken bağımlılık grafiği (`sw_top_finder`) taranır:
> *"Bu montajın 3 alt parçası güncel değil. Kilitlemeden önce güncelleyelim mi?"*
> **[Ağacın tamamını güncelle]**  [Sadece montajı kilitle]

### 14.3 Bayatlık görünürlüğü (UI)

"Çek" modelinde kullanıcı ne kadar geride olduğunu **her an** görebilmeli:

- Üst çubukta rozet: **"⬇ 3 yeni değişiklik"** → tıklayınca gelen commit'ler listelenir
  (kim, ne zaman, hangi dosyalar, mesaj, önizleme küçük resmi) → **[Hepsini al]**
- Dosya listesinde satır bazlı durum ikonu:
  `✓ güncel` · `⬇ daha yenisi var` · `🔒 Ahmet'te kilitli` · `👁 Ahmet açtı (düzenlemedi)` ·
  `✎ sende kilitli, kaydedilmemiş değişiklik var`
- SolidWorks'te **bayat bir dosya açılırsa** uyarı: *"Bu dosyanın daha yeni sürümü var."*

### 14.4 Bildirim türleri
| Olay | Kime | Şiddet |
|---|---|---|
| Yeni commit geldi | herkese | sessiz rozet |
| Kilit alındı | herkese | sessiz |
| Kilit bırakıldı | herkese | sessiz |
| Kilit bırakıldı + sen kuyruktaydın | kuyruktakine | **belirgin + tek tık kilitle** (30 sn öncelik) |
| Kilidin zorla kırıldı | sahibine | **belirgin uyarı** |
| Kilitlemek istedin ama bayatsın | sana | engelleyici diyalog |
| Peer katıldı / ayrıldı | herkese | sessiz |
| İraksama tespit edildi | iki tarafa | **engelleyici — çift onay ekranı** |
