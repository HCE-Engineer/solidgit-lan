# SolidGit LAN — Özellik Havuzu

Aklıma gelen her şey. Hepsi yapılacak diye bir şey yok — bu bir **menü**, yol haritası değil.
Seçerken kullanacağın ölçüt şu olmalı: *"bu olmasa takım ne kaybeder?"*

**Zorluk:** ⬤ küçük (yarım gün) · ⬤⬤ orta (1-2 gün) · ⬤⬤⬤ büyük (3+ gün) · ⬤⬤⬤⬤ proje

**Durum:** ✅ yapıldı · 🔨 kısmen · ⬜ yok

---

## A. Sürüm kontrolü çekirdeği

| # | Özellik | Ne işe yarar | Zorluk | Durum |
|---|---|---|---|---|
| A1 | Tek dosyayı eski sürüme döndür | "Bu parçayı dünkü haline al" — en sık istenecek şey | ⬤ | 🔨 çekirdek var, UI yok |
| A2 | Tüm projeyi bir ana döndür (zaman tüneli) | "Salı sabahına dönelim" | ⬤ | 🔨 `checkout` var, UI yok |
| A3 | İki kaydı karşılaştır | Hangi dosyalar değişmiş, kim yapmış | ⬤⬤ | 🔨 `reconcile.diff` var |
| A4 | Etiket / sürüm damgası | "Rev-B — müşteriye giden", "yarışma teslimi" | ⬤ | ⬜ |
| A5 | Son kaydı geri al (undo commit) | Yanlış mesaj, erken kayıt | ⬤ | ⬜ |
| A6 | Kayıt mesajını düzelt | Yazım hatası | ⬤ | ⬜ |
| A7 | Eski kayıttan tek dosya çek | "O montajın eski gövdesini istiyorum" | ⬤ | ⬜ |
| A8 | Geçmişte arama | Mesaja/kişiye/dosyaya göre filtre | ⬤ | ⬜ |
| A9 | Dosya geçmişi | "Bu parçaya kim, ne zaman, kaç kez dokundu" | ⬤⬤ | ⬜ |
| A10 | Silinen dosyayı geri getir | Kaza sonrası kurtarma | ⬤ | ⬜ |
| A11 | **Otomatik ara kayıt** | Her N dakikada değişiklik varsa sessiz snapshot. Öğrenci ekibinde en çok iş kurtaracak özellik bu olabilir | ⬤⬤ | ⬜ |
| A12 | Çöp toplama + saklama politikası | "Son 50 kaydı tut" — disk şişmesin | ⬤ | 🔨 `gc` var, UI/politika yok |
| A13 | Deneme dalı (geçici) | "Farklı bir braket deneyeyim, tutmazsa atarım" | ⬤⬤⬤ | ⬜ |
| A14 | Kayıt öncesi doğrulama | Klasör dışı referans, eksik dosya kontrolü | ⬤⬤ | ⬜ |
| A15 | Depo bütünlük kontrolü / onarım | Bozuk blob'u tespit et, peer'dan yeniden çek | ⬤⬤ | ⬜ |

---

## B. Kilit ve iş birliği

| # | Özellik | Ne işe yarar | Zorluk | Durum |
|---|---|---|---|---|
| B1 | Dosya kilidi | Aynı parçayı iki kişi düzenlemesin | ⬤⬤ | ✅ yerel |
| B2 | **Kilidi ağ üzerinden paylaş** | Asıl değer burada | ⬤⬤ | ✅ |
| B3 | Yumuşak talep | "Ahmet bu dosyayı açtı ama düzenlemedi" | ⬤ | ✅ (SolidWorks'e bağlanması D2'de) |
| B4 | Kilit kuyruğu + 30 sn öncelik | "Serbest kalınca bana haber ver" | ⬤ | ✅ çekirdek+ağ, UI butonu yok |
| B5 | Kiralama + otomatik düşme | Kapanan laptop dosyayı rehin almasın | ⬤ | ✅ |
| B6 | Zorla kilit kırma + bildirim | Yönetici müdahalesi, iz bırakarak | ⬤ | 🔨 çekirdek var, UI yok |
| B7 | Alt-montaj ağacını tek hamlede kilitle | "Şasi bende" = 12 dosya | ⬤⬤ | ⬜ |
| B8 | Kilit notu | "delik açıyorum, 1 saat sürer" | ⬤ | ⬜ |
| B9 | "Bırakır mısın?" isteği | Mesajlaşmadan halletmek | ⬤ | ⬜ |
| B10 | Kimler çevrimiçi paneli | Takımın canlı görünümü | ⬤ | 🔨 peer listesi var |
| B11 | Dosya ataması | "Bu parça Ahmet'te" — kilitten bağımsız sorumluluk | ⬤⬤ | ⬜ |
| B12 | Roller (yönetici / katkıcı / izleyici) | Hoca veya müşteri sadece baksın | ⬤ | 🔨 protokolde var |
| B13 | Uygulama içi kısa mesaj | "gövdeyi bitirdim, senin sıran" | ⬤⬤ | ⬜ |

---

## C. Ağ ve senkronizasyon

| # | Özellik | Ne işe yarar | Zorluk | Durum |
|---|---|---|---|---|
| C1 | Cihaz keşfi (mDNS + yayın) | Birbirinizi görün | ⬤⬤ | ✅ |
| C2 | Katılım kodu + onay + TLS | Güvenli katılım | ⬤⬤⬤ | ✅ |
| C3 | **Dosya aktarımı** | "Herkeste aynı dosya" — projenin asıl vaadi | ⬤⬤⬤ | ✅ |
| C4 | İlerleme çubuğu + kaldığı yerden devam | 200 MB'ta şart | ⬤⬤ | ✅ |
| C5 | Iraksama uzlaştırma + çift onay | İki kişi ayrı çalışıp buluşunca | ⬤⬤⬤ | ✅ |
| C6 | Değişen blok aktarımı (chunk) | Küçük değişiklikte 200 MB göndermemek | ⬤⬤⬤ | ⬜ |
| C7 | Sürü aktarımı (swarm) | 4 kişiye dağıtımda 3-4x hız | ⬤⬤⬤ | ⬜ |
| C8 | Koordinatör devri / yeniden seçim | Host çıkınca hayat devam etsin | ⬤⬤ | ⬜ |
| C9 | **USB ile aktarım (sneakernet)** | Ağ hiç çalışmazsa: paketi USB'ye at, karşıda içe aktar. Depomuz içerik-adresli olduğu için neredeyse bedava | ⬤⬤ | ⬜ |
| C10 | Güvenlik duvarı sihirbazı | "Arkadaşlarım göremiyor" 1 numaralı şikâyet | ⬤⬤ | ⬜ |
| C11 | Ağ tanılama paneli | Adres, port, duvar, keşif — hepsi tek ekranda test | ⬤⬤ | 🔨 kısmen |
| C12 | Elle IP ile bağlan | Keşif çalışmazsa son çare | ⬤ | 🔨 CLI'da var |
| C13 | Bant genişliği sınırı | Hotspot'u boğmamak | ⬤ | ⬜ |
| C14 | Aktarım önceliği | "Önce şu montajı ver" | ⬤⬤ | ⬜ |
| C15 | Bağlantı kalitesi göstergesi | Yavaşlığın sebebini görmek | ⬤ | ⬜ |
| C16 | Çevrimdışı çalış → sonra uzlaş | Evde çalış, okulda birleştir | ⬤⬤ | 🔨 çekirdek hazır |

---

## D. SolidWorks entegrasyonu

Bu bölüm projeyi "Dropbox"tan ayıran şey. Kodun bir kısmı eski SolidGit projesinde **canlı test
edilmiş** halde duruyor, taşınmayı bekliyor.

| # | Özellik | Ne işe yarar | Zorluk | Durum |
|---|---|---|---|---|
| D1 | Açık belgeleri izleme | Neyin açık olduğunu bilmek | ⬤⬤ | 🔨 eski projede hazır |
| D2 | **Açınca otomatik kilit** | Kullanıcı hiçbir şey yapmadan korunur | ⬤⬤ | ⬜ |
| D3 | Kaydetmeden kapatınca kilidi bırak | Kilit çöplüğü olmasın | ⬤⬤ | ⬜ |
| D4 | Kaydedilmemiş değişiklik varken senkronu engelle | Veri kaybını önler | ⬤ | ⬜ |
| D5 | Uygulamadan SolidWorks'te aç | Tek tık | ⬤ | ⬜ |
| D6 | eDrawings ile aç | SolidWorks'ü olmayan arkadaş | ⬤ | ⬜ |
| D7 | **Gömülü önizleme küçük resmi** | Dosya adına bakmadan "bu hangisi" — UX'te en büyük sıçrama | ⬤⬤⬤ | ⬜ |
| D8 | Özel özellikler (parça no, revizyon, malzeme, ağırlık) | Liste anlamlı hale gelir | ⬤⬤ | ⬜ |
| D9 | Bağımlılık grafiği görünümü | Montaj ağacını görmek | ⬤⬤ | 🔨 eski projede hazır |
| D10 | Üst seviye montajları bul | "Hangisi ana montaj?" | ⬤ | 🔨 eski projede hazır |
| D11 | Pack and Go ile içe aktarma | Kapalı klasör garantisi + Toolbox çözümü | ⬤⬤⬤ | ⬜ |
| D12 | Klasör dışı referans denetçisi | Kapalı klasör ilkesini zorlar | ⬤⬤ | ⬜ |
| D13 | Toplu dışa aktarma (PDF/DXF/STEP) | Teslim paketi | ⬤⬤⬤ | ⬜ |
| D14 | BOM → Excel | Malzeme listesi | ⬤⬤⬤ | ⬜ |
| D15 | **Kütle/ağırlık takibi** | Her kayıtta kütle özelliklerini sakla → "montaj 3 haftada 12 kg arttı" grafiği. Yarışma/proje ekibinde çok değerli | ⬤⬤ | ⬜ |
| D16 | Teknik resim–model uyumsuzluğu uyarısı | "Resim eski modele göre" | ⬤⬤ | ⬜ |
| D17 | Rebuild hatası tespiti | Bozuk montajı erken yakala | ⬤⬤ | ⬜ |
| D18 | Konfigürasyon farkındalığı | Aynı dosya, farklı konfigürasyon | ⬤⬤⬤ | ⬜ |

---

## E. Arayüz ve kullanılabilirlik

| # | Özellik | Ne işe yarar | Zorluk | Durum |
|---|---|---|---|---|
| E1 | Dosya listesi + durum renkleri | Temel görünüm | ⬤⬤ | ✅ |
| E2 | Klasör ağacı görünümü | 200 dosyada düz liste yetmez | ⬤⬤ | ⬜ |
| E3 | Arama / filtre kutusu | "govde" yaz, bul | ⬤ | ⬜ |
| E4 | Sütuna göre sırala | Boyut, tarih, durum | ⬤ | ⬜ |
| E5 | Hızlı filtreler | "sadece değişenler", "sadece bendekiler" | ⬤ | ⬜ |
| E6 | Küçük resimli görünüm | D7'ye bağlı, kartlı düzen | ⬤⬤ | ⬜ |
| E7 | Zaman çizelgesi görünümü | Geçmişi tarih şeridi olarak | ⬤⬤ | ⬜ |
| E8 | Yan yana önizleme karşılaştırma | İki sürümün resmi | ⬤⬤ | ⬜ |
| E9 | Bildirimler (Windows bildirim balonu) | Uygulama arkadayken haber ver | ⬤⬤ | ⬜ |
| E10 | Sistem tepsisi simgesi | Kapatmadan arkada çalışsın | ⬤⬤ | ⬜ |
| E11 | "Geri al" bildirimi | "Kilit bırakıldı — geri al" | ⬤ | ⬜ |
| E12 | Klasörü sürükle-bırak ile aç | Küçük ama hoş | ⬤ | ⬜ |
| E13 | Son kullanılan projeler | Her seferinde klasör aramamak | ⬤ | ✅ |
| E14 | Projeler arası hızlı geçiş | Birden fazla iş | ⬤⬤ | ⬜ |
| E15 | Klavye kısayolları | Ctrl+S = kaydet vb. | ⬤ | ⬜ |
| E16 | İlk açılış turu | 4 adımlık tanıtım | ⬤⬤ | 🔨 "Nasıl çalışır?" var |
| E17 | Pencere boyutu/konumu hatırlama | Küçük konfor | ⬤ | ⬜ |
| E18 | Açık/koyu tema | Tercih | ⬤ | ⬜ |
| E19 | Türkçe/İngilizce | Takıma göre | ⬤⬤ | ⬜ |
| E20 | Yazı boyutu / erişilebilirlik | Yüksek DPI ekranlar | ⬤ | ⬜ |
| E21 | Adım adım "şimdi ne yapmalıyım" ipucu | Yeni kullanıcıyı yalnız bırakmamak | ⬤ | ✅ |

---

## F. Güvenlik ağı (veri kaybına karşı)

| # | Özellik | Ne işe yarar | Zorluk | Durum |
|---|---|---|---|---|
| F1 | Atomik yazma (tmp→fsync→rename) | Yarım dosya asla oluşmaz | ⬤⬤ | ✅ |
| F2 | Her okumada hash doğrulama | Sessiz bozulma yakalanır | ⬤ | ✅ |
| F3 | Salt-okunur zorlama | Kilitsiz dosya kazara değişmez | ⬤ | ✅ |
| F4 | Oturum kaydı + hata günlüğü | Sorunu tahmin etmek yerine görmek | ⬤⬤ | ✅ |
| F5 | **Dış diske otomatik yedek** | Her kayıtta ikinci bir kopya | ⬤⬤ | ⬜ |
| F6 | Kayıt öncesi disk alanı kontrolü | "Disk dolu" sürprizi | ⬤ | ⬜ |
| F7 | Silmek yerine çöp kutusu | Geri dönülebilir silme | ⬤ | ⬜ |
| F8 | Yıkıcı işlemlerde onay | "Emin misin?" | ⬤ | 🔨 kısmen |
| F9 | Yönetici işlemleri denetim kaydı | Kim neyi zorla açtı | ⬤ | ⬜ |
| F10 | Çökme sonrası kurtarma | Yarım aktarımı sürdür | ⬤⬤ | 🔨 temel var |

---

## G. Proje yönetimi

| # | Özellik | Ne işe yarar | Zorluk | Durum |
|---|---|---|---|---|
| G1 | Proje notları / açıklama | "Bu proje ne, kim ne yapıyor" | ⬤ | ⬜ |
| G2 | Dosyaya not / yorum | "Bu delik M6 olacak" | ⬤⬤ | ⬜ |
| G3 | Revizyon numaralandırma (A, B, C) | Mühendislik pratiği | ⬤⬤ | ⬜ |
| G4 | Onay akışı (tasarımcı → kontrol → onay) | Ciddi projelerde şart | ⬤⬤⬤ | ⬜ |
| G5 | Teslim paketi dışa aktarma | PDF + STEP + BOM tek zip | ⬤⬤⬤ | ⬜ |
| G6 | Teslim öncesi kontrol listesi | "Hepsi kaydedildi mi, kilit kaldı mı" | ⬤ | ⬜ |
| G7 | Takım aktivite raporu | Kim ne kadar katkı verdi (hoca için) | ⬤⬤ | ⬜ |
| G8 | Parça başına süre takibi | Kilit süresinden otomatik | ⬤⬤ | ⬜ |

---

## H. İleri / uzak gelecek

| # | Özellik | Zorluk |
|---|---|---|
| H1 | İnternet varken buluta yedek köprüsü | ⬤⬤ |
| H2 | Host'un servis ettiği web arayüzü (sunucu zaten HTTP konuşuyor) | ⬤⬤ |
| H3 | Telefondan izleyici | ⬤⬤⬤ |
| H4 | Tek exe paketleme + imzalama | ⬤⬤ |
| H5 | Otomatik güncelleme (host yeni sürümü dağıtsın) | ⬤⬤⬤ |
| H6 | Eklenti / betik desteği | ⬤⬤⬤⬤ |
| H7 | Okul/şirket dosya sunucusuyla entegrasyon | ⬤⬤⬤ |

---

## Benim önerim — sıradaki 10

Sırayla, gerekçeleriyle:

1. **C3 — Dosya aktarımı.** Projenin tek cümlelik vaadi bu ("herkeste aynı dosya olacak") ve
   hâlâ yok. Bundan önce yapılan her şey buna hizmet ediyor.
2. **B2 — Kilidi ağ üzerinden paylaş.** Kilit tek makinede anlamsız. C3 ile birlikte uygulama
   ilk kez gerçekten işe yarar hale gelir.
3. **C10 — Güvenlik duvarı sihirbazı.** İlk gerçek denemede takılacağın yer burası; kendi
   oturum kaydın da hatanın nasıl gizlendiğini gösterdi.
4. **A11 — Otomatik ara kayıt.** Öğrenci ekibinde en çok iş kurtaracak tek özellik. Ucuz.
5. **D7 — Önizleme küçük resmi.** Arayüzde en büyük sıçrama. `govde.sldprt` yerine parçanın
   resmini görmek her şeyi değiştirir.
6. **A1 + A2 — Geri alma arayüzü.** Çekirdek zaten var, sadece ekran lazım. "Geri dönebiliyorum"
   hissi olmadan kimse bu araca güvenmez.
7. **C9 — USB ile aktarım.** Ağ er ya da geç çalışmayacak. Depomuz içerik-adresli olduğu için
   bu neredeyse bedava geliyor ve seni tamamen tıkanmaktan kurtarır.
8. **D2/D3 — SolidWorks otomatik kilit.** Kullanıcı hiçbir şey yapmadan korunur; eski projede
   kodun çoğu hazır.
9. **E2 + E3 — Klasör ağacı + arama.** 12 dosyada düz liste iyi, 200 dosyada işkence.
10. **D15 — Kütle takibi.** Ucuz, benzersiz ve mühendislik projesinde gerçekten kullanılır.

**Bilerek dışarıda bıraktıklarım:** C6/C7 (chunk + swarm) — 3-4 kişi/200 MB ölçeğinde kazanç
yok, sonra eklenebilir. G4 (onay akışı) — öğrenci ekibi için fazla ağır. A13 (deneme dalı) —
kavramsal olarak en riskli özellik, kullanıcı kafası karışır.
