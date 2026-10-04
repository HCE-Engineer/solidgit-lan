# SolidGit LAN — Ağ Protokolü v1

**Protokol sürümü:** `1`
**Durum:** kod yazılmadan önce yazılan sözleşme; büyük kısmı uygulandı.

> **Uygulamadan farklar:** §9 olay akışı (WebSocket) yapılmadı — katılanlar kilit tablosunu
> iki saniyede bir sorguluyor ve bildirimler bu sorgunun yanıtına ekleniyor. §10 koordinatör
> seçimi yapılmadı — oturumu açan kişi çıkarsa oturum biter. Birleştirme onayı uçla değil,
> host'un ekranındaki butonla verilir; öneren taraf `/v1/reconcile/status` ile bekler.

> **Neden kodadan önce bu doküman?**
> Takım arkadaşının makinesinde er ya da geç **eski sürüm** olacak. Uygulama uyumsuz sürümü
> gördüğünde "güncelle" demeli, bozuk davranmamalı. Bunu sonradan eklemek imkânsıza yakındır.

---

## 1. Genel kurallar

- Taşıma: **HTTPS (kendi imzalı sertifika) + WebSocket**, koordinatör dinler, peer'lar bağlanır.
- Gövde formatı: JSON (UTF-8). İstisna: blob aktarımı → ham ikili (`application/octet-stream`).
- Her istekte zorunlu başlıklar:
  ```
  X-SG-Protocol: 1
  X-SG-Repo: <repo_id>
  Authorization: Bearer <device_token>
  ```
- **Sürüm uyuşmazlığı:** Sunucu farklı bir `X-SG-Protocol` görürse `426 Upgrade Required` döner
  ve `{"server_protocol": 1, "min_supported": 1}` gövdesini verir. İstemci kullanıcıya
  *"Takım arkadaşının sürümü farklı"* der, bağlanmaya çalışmaz.
- Tüm hash'ler: **sha256**, küçük harf hex.
- Tüm zamanlar: ISO-8601 + zaman dilimi. **Zaman damgaları sadece gösterim içindir**,
  hiçbir karar duvar saatine dayanmaz (bkz. `lamport`).

### 1.1 Hata gövdesi (tüm hatalar için ortak)
```json
{ "error": "LOCK_STALE", "message": "gövde.sldprt'nin daha yeni sürümü var.", "detail": { } }
```

| Kod | Anlam |
|---|---|
| `PROTOCOL_MISMATCH` | Sürüm uyuşmuyor |
| `REPO_MISMATCH` | Farklı `repo_id` — alakasız proje |
| `NOT_APPROVED` | Cihaz henüz host tarafından onaylanmadı |
| `LOCK_HELD` | Kilit başkasında |
| `LOCK_STALE` | Güncel sürüme sahip değilsin (bkz. §6.1) |
| `LOCK_NOT_OWNED` | Sahibi olmadığın kilidi bırakmaya/kullanmaya çalıştın |
| `DIVERGED` | Geçmişler ıraksadı → uzlaştırma gerekli |
| `OBJECT_MISSING` | İstenen blob depoda yok |
| `NOT_COORDINATOR` | Bu düğüm artık koordinatör değil (yeniden seçim oldu) |

---

## 2. Keşif (Discovery)

**Birincil — mDNS/Zeroconf:** servis tipi `_solidgit._tcp.local.`

TXT kayıtları:
| Anahtar | Örnek | Açıklama |
|---|---|---|
| `proto` | `1` | protokol sürümü |
| `repo` | `4f2a9c1b…` | repo_id |
| `name` | `Montaj1` | proje adı (gösterim) |
| `user` | `Hüseyin` | kullanıcı adı (gösterim) |
| `dev` | `HUSEYIN-PC` | cihaz adı |
| `role` | `host` \| `peer` | rol |
| `head` | `c000342` | şu anki HEAD |
| `fp` | `9C1B4F2A` | TLS sertifika parmak izinin ilk 8 hanesi |

**Yedek — UDP broadcast:** aynı alanlar JSON olarak, 2 saniyede bir, port `7800`.
Bazı hotspot sürücüleri multicast'i boğduğu için **ikisi birden** çalışır.

**Son çare:** elle IP + port girme.

---

## 3. Güvenlik ve katılım

### 3.1 Katılım kodu
Host ekranda gösterir: **`SG-4F2A-9C1B`** — 8 karakterlik rastgele bir sır.

Alfabede **I, L, O, U yok**: odanın öbür ucundan okunan bir kodda 0/O karışıklığına on dakika
kaybedilmemeli. İstemci tarafında `I→1`, `L→1`, `O→0`, `U→V` otomatik düzeltiliyor, tire ve
`SG` öneki isteğe bağlı.

Anahtar türetme: `scrypt(kod, salt=repo_id, n=2**14)`. Düz hash değil — 8 karakter, aday
denemesi ucuzsa tahmin edilebilir; scrypt bunu bizim için ucuz, saldırgan için pahalı tutar.

### 3.2 Katılım akışı — karşılıklı kanıt, sertifikaya bağlı

```
Peer                                        Koordinatör
 │                                              │
 ├─ TLS: sertifikayı çek, parmak izini hesapla ▶│   (henüz güvenmedik, sadece sabitledik)
 │                                              │
 ├─ GET /v1/info ─────────────────────────────▶ │   (kimlik doğrulaması gerektirmez)
 │ ◀────────── repo_id, proto, head, name ──────┤
 │                                              │
 ├─ POST /v1/join/begin {nonce_c, device} ────▶ │
 │ ◀─ {nonce_s, server_proof, fingerprint} ─────┤   ⬅ HOST ÖNCE KANITLIYOR
 │                                              │
 │  server_proof doğrulanır. Tutmazsa BURADA    │
 │  durulur — kendi kanıtımızı hiç göndermeyiz. │
 │                                              │
 ├─ POST /v1/join/complete {client_proof} ────▶ │
 │                                              ├─▶ 🔔 HOST EKRANINDA ONAY DİYALOĞU
 │ ◀──────── {status: "pending"} ───────────────┤     "AHMET-LAPTOP katılmak istiyor"
 │                                              │     [İzin Ver] [Reddet] [Hep izin ver]
 ├─ GET /v1/join/status?device_id= ───────────▶ │
 │ ◀── {status:"approved", device_token} ───────┤
 └─ Artık normal istemci                        │
```

**Kanıt formülü:**
`HMAC-SHA256(key, "server"|"client" ‖ nonce_c ‖ nonce_s ‖ tls_fingerprint)`

Üç önemli nokta:

1. **Host önce kanıtlar.** Host taklit eden bir makine, biz ona hiçbir şey vermeden yakalanır.
2. **Kanıt TLS sertifikasının parmak izine bağlı (channel binding).** Araya giren biri kodu
   bir şekilde bilse bile farklı bir sertifika sunduğu için kanıt tutmaz. Bu sayede parmak
   izini katılım koduna gömmeye gerek kalmıyor — kod kısa kalabiliyor.
3. **Kod doğru olsa bile host onayı zorunlu.** Doğru kriptografi, "bu cihaz gerçekten takım
   arkadaşımın mı" sorusunun yerine geçmez.

### 3.3 Onaylanan cihazlar nerede saklanır?
`%LOCALAPPDATA%\SolidGitLAN\devices\<repo_id>.json` — **çalışma alanının İÇİNDE DEĞİL.**

Bu kayıtlar taşınabilir kimlik bilgisi (bearer token) içerir; çalışma alanı klasörü ise
insanların USB'ye atıp elden ele gezdirdiği bir şeydir. Güven kararı **bu makineye** aittir,
projeye değil.

### 3.3 Roller
| Rol | Yetkiler |
|---|---|
| `admin` (host) | her şey + zorla kilit kırma + cihaz onayı + koordinatör devri |
| `contributor` | kilit al/bırak, commit, çek |
| `viewer` | sadece çek ve görüntüle |

---

## 4. Uç noktalar — Bilgi ve geçmiş

### `GET /v1/info` *(kimlik doğrulaması gerektirmez)*
```json
{ "protocol": 1, "repo_id": "4f2a9c1b…", "name": "Montaj1",
  "head": "c000342", "lamport": 342, "role": "host",
  "user": "Hüseyin", "device": "HUSEYIN-PC", "peer_count": 3 }
```

### `POST /v1/join/begin` · `POST /v1/join/complete` · `GET /v1/join/status`
Bkz. §3.2. `begin` protokol sürümünü ve `repo_id`'yi doğrular (uyumsuzsa `426` / `409`),
`complete` istemci kanıtını sınar (`401 BAD_JOIN_CODE`), `status` onay beklerken yoklanır.

### `GET /v1/commits?since=<commit_id>`
`since` verilmezse tüm zincir. Yalnızca **meta** döner, dosya içeriği değil.
```json
{ "head": "c000342",
  "commits": [ { "id": "c000342", "parents": ["c000341"], "lamport": 342,
                 "author": {"name":"Ahmet","device":"AHMET-LAPTOP"},
                 "wall_clock": "2026-08-08T14:32:00+03:00",
                 "message": "Gövde kalınlığı 3mm",
                 "files": { "Montaj1/govde.sldprt": {"sha256":"a3f9…","size":4821004} } } ] }
```

### `GET /v1/commit/<id>` — tek commit'in tam manifesti

---

## 5. Uç noktalar — Nesne (blob) aktarımı

### `POST /v1/objects/missing`
"Bende bu hash'ler var, hangileri sende yok?" — **ters yön de aynı uç nokta**, senkron
simetriktir.
```json
{ "have": ["a3f9…", "9b1d…", "c7e2…"] }
→ { "missing": ["c7e2…"] }
```

### `GET /v1/objects/<sha256>`
- `Content-Type: application/octet-stream`
- **`Range` başlığı desteklenir** → kaldığı yerden devam etme bedava gelir.
- Gövde **zstd** sıkıştırılmıştır; `X-SG-Encoding: zstd`, `X-SG-Raw-Size: 4821004`.
- İstemci indirdikten sonra **hash'i doğrular**; tutmazsa dosyayı atar ve tekrar dener.

### `PUT /v1/objects/<sha256>`
Peer'ın koordinatöre yeni blob yüklemesi. Koordinatör hash'i doğrulamadan kabul etmez.
Yazma **atomiktir**: `tmp/` → fsync → rename. Yarım blob asla `objects/`'e girmez.

---

## 6. Uç noktalar — Kilit

### `POST /v1/locks/acquire` — **ATOMİK: hep ya da hiç**
```json
{ "paths": ["Montaj1/govde.sldprt", "Montaj1/kapak.sldprt"],
  "base": { "Montaj1/govde.sldprt": "a3f9…", "Montaj1/kapak.sldprt": "9b1d…" },
  "lease_seconds": 14400 }
```
`base` = istemcinin **elindeki** hash'ler. Koordinatör her biri için son commit'teki hash ile
karşılaştırır.

**Başarı** → `200`, tüm kilitler verildi.
**Başarısızlık** → `409`, **hiçbiri verilmez** (kısmen verilenler geri alınır):
```json
{ "error": "LOCK_STALE",
  "detail": { "stale": [ {"path":"Montaj1/govde.sldprt",
                          "your": "a3f9…", "latest": "e1b7…",
                          "by": "Ahmet", "at": "2026-08-08T14:32:00+03:00"} ],
              "held":  [ {"path":"Montaj1/kapak.sldprt", "by":"Mehmet",
                          "since":"2026-08-08T13:10:00+03:00"} ] } }
```

> ⚠️ Kısmi başarı **yoktur**. 12 kilitten 9'u verilip 3'ü verilemezse 9'u da geri alınır —
> yoksa karşılıklı kilitlenme (deadlock) oluşur ve kimse neden takıldığını anlamaz.

### `POST /v1/locks/release`
```json
{ "paths": [...], "reason": "committed" | "closed_without_saving" | "manual" }
```

### `POST /v1/locks/renew` — kiralama kalp atışı
Uygulama açıkken 60 saniyede bir. Yenilenmezse kilit süresi dolunca **otomatik düşer**
(laptop öldü / SolidWorks çöktü senaryosu).

### `POST /v1/locks/soft-claim`
Dosya SolidWorks'te **açıldığı anda**, henüz düzenlenmeden. Kimseyi engellemez, sadece
`soft_claim` olayı yayınlar. Bu, iki kişinin 20 dakika boşuna çalışmasını engeller.

### `POST /v1/locks/queue`
"Serbest kalınca haber ver." Kilit bırakıldığında kuyruktaki ilk kişiye
**30 saniyelik öncelik penceresi** verilir — o sürede başkasının isteği reddedilir.
Yoksa "kim daha hızlı tıkladı" yarışı olur.

### `GET /v1/locks` — tam kilit tablosu (yeniden bağlanmada durum tazeleme)

---

## 7. Uç noktalar — Commit

### `POST /v1/commits`
```json
{ "parent": "c000342", "message": "Gövde kalınlığı 3mm",
  "files": { "Montaj1/govde.sldprt": {"sha256":"e1b7…","size":4830112} } }
```
**Ön koşullar (koordinatör doğrular):**
1. Değişen her dosyanın kilidi istekçide olmalı → yoksa `LOCK_NOT_OWNED`
2. `parent == HEAD` olmalı → değilse `DIVERGED`
3. Tüm bloblar zaten yüklenmiş olmalı → yoksa `OBJECT_MISSING`

Başarıda: yeni commit yazılır, HEAD ilerler, tüm peer'lara `new_commit` olayı yayınlanır
(**veri itilmez, sadece haber verilir** — çekme modeli).

---

## 8. Uç noktalar — Uzlaştırma (iraksama)

### `POST /v1/reconcile/propose`
Iraksama tespit edildiğinde bir taraf plan üretir:
```json
{ "plan_id": "r-8f21",
  "mine_head": "c000342", "theirs_head": "c000399",
  "resolution": { "Montaj1/govde.sldprt": "theirs",
                  "Montaj1/kapak.sldprt": "mine",
                  "Montaj1/mil.sldprt":   "theirs" } }
```

### `POST /v1/reconcile/approve` — **ÇİFT ONAY**
Karşı taraf **aynı** `plan_id`'yi onaylamadan tek bayt yazılmaz.
```json
{ "plan_id": "r-8f21", "approve": true }
```
Sonuç: **iki ebeveynli** uzlaşma commit'i. Kaybeden sürüm **silinmez** — depoda kalır,
geçmişten geri alınabilir.

`repo_id` farklıysa uzlaştırma hiç başlamaz → `REPO_MISMATCH`.

---

## 9. Olay akışı — `WS /v1/events`

Sunucudan istemciye tek yönlü bildirimler (istemci polling yapmaz):

```json
{ "type": "lock_released", "path": "Montaj1/govde.sldprt",
  "by": "Ahmet", "at": "2026-08-08T14:32:00+03:00", "seq": 1042 }
```

| `type` | Ne zaman |
|---|---|
| `peer_joined` / `peer_left` | cihaz katıldı/ayrıldı |
| `join_request` | *(sadece admin'e)* onay bekleyen cihaz |
| `join_approved` | katılım onaylandı, `device_token` içerir |
| `soft_claim` / `soft_claim_cleared` | dosya SolidWorks'te açıldı/kapandı |
| `lock_acquired` / `lock_released` / `lock_expired` | kilit değişimi |
| `lock_forced` | yönetici kilidi kırdı *(sahibine belirgin uyarı)* |
| `lock_available_for_you` | kuyruktaydın, sıra sende *(30 sn öncelik)* |
| `new_commit` | yeni commit var *(veri değil, sadece haber)* |
| `coordinator_changed` | koordinatör devredildi / yeniden seçildi |
| `diverged` | ıraksama tespit edildi *(engelleyici)* |

**`seq` alanı zorunlu:** bağlantı koparsa istemci `WS /v1/events?since=<seq>` ile bağlanır ve
kaçırdığı olayları alır. Sunucu son 1000 olayı bellekte tutar; daha geriye düşülürse istemci
tam durum tazelemesi yapar (`GET /v1/locks` + `GET /v1/commits?since=`).

---

## 10. Koordinatör seçimi

Koordinatör kaybolduğunda (3 kalp atışı kaçtı):
1. Kalan peer'lar `GET /v1/info` ile birbirlerinin `lamport` değerlerini toplar.
2. Aday = **en yüksek `lamport`** (en çok geçmişe sahip olan). Eşitlikte en düşük `device_id`.
3. Aday kullanıcıya sorar: *"Koordinatör düştü. Bu görevi ben üsteneyim mi?"* → **elle onay.**
4. Yeni koordinatör kilit tablosunu son bilinen halinden kurar ve **kilit affı** ilan eder:
   şüpheli kilitler kullanıcılara gösterilir, otomatik düşürülmez.

> Otomatik devralma bilinçli olarak **yapılmıyor** — split-brain (iki koordinatör) riski,
> bir onay tıklamasından çok daha pahalıdır.

---

## 11. Sabitler

| Sabit | Değer | Gerekçe |
|---|---|---|
| Kilit kiralama süresi | 4 saat | bir çalışma seansı |
| Kiralama yenileme | 60 sn | ölü düğümü hızlı yakala |
| Koordinatör kalp atışı | 5 sn | 3 kayıpta seçim = 15 sn |
| Kuyruk öncelik penceresi | 30 sn | tıklama yarışını engelle |
| Keşif broadcast | 2 sn | canlı peer listesi hissi |
| Olay tamponu | son 1000 olay | kısa kopmaları kurtarır |
| Blob parça boyutu | 4 MB | ilerleme çubuğu + devam ettirme |
| Varsayılan port | 7800 (UDP keşif) / 7801 (HTTPS) | |
