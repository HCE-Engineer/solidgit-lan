# 5 dakikada ilk oturum

🇬🇧 [English](TUTORIAL.en.md) · ← [README](../README.md)

İki kişiyle anlatıyoruz: **Hüseyin** montajı olan ve oturumu açan kişi, **Ahmet** ise dosyaları
henüz olmayan takım arkadaşı. Üçüncü, dördüncü kişi Ahmet'in adımlarını tekrarlar.

> **Hazırlık:** Herkeste uygulama olsun — ya `SolidGitLAN.bat` ile kaynaktan ya da
> **Sürüm oluştur…** ile üretilen zip'ten. Herkesin sürümü aynı olmalı; farklı sürümler
> birbirine bağlanmayı reddeder.

---

## Hüseyin — projeyi paylaşır

### 1. Montaj klasörünü aç

**Klasör seç…** → montaj klasörünü göster. Klasör henüz proje değilse sorar; "Evet" de.
Dosyaların değişmez, yalnızca gizli bir `.solidgit` klasörü eklenir.

Dosyalar sayfasında hepsi **Yeni** görünür. Alttaki kutuya kısa bir isim yaz
("başlangıç" yeter) ve **Değişiklikleri kaydet**'e bas. Artık her dosya **Güncel**.

> Kural: parçalar, montajlar ve teknik resimler **tek bir klasörün içinde** olmalı; klasör
> dışına referans verilmemeli. Toolbox parçalarını da klasöre kopyala.

### 2. Oturumu başlat

Bilgisayarının hotspot'unu aç (ya da hepiniz aynı Wi-Fi'a bağlanın). **Ağ** →
**Oturumu başlat**. Büyük harflerle bir kod çıkar; onu arkadaşlarına söyle.

![Oturum açık, katılım kodu görünüyor](images/adim2-oturum.png)

İlk seferde Windows Güvenlik Duvarı izin isteyebilir — **Özel ağlar**'a izin ver.

---

## Ahmet — katılır ve indirir

### 3. "Arkadaşının oturumuna katıl"

Uygulamayı aç. Proje gerekmez — doğrudan **Arkadaşının oturumuna katıl**'a bas.

![Karşılama ekranı: Klasör seç ya da katıl](images/adim1-karsilama.png)

### 4. Oturumu seç, kodu gir

Hüseyin'in oturumu listede kendiliğinden görünür. Seç → **Seçili oturuma katıl** → kodu yaz.

![Oturum listede, seçili](images/adim3-katil.png)

Bu sırada Hüseyin'in ekranında bir istek belirir. **Hüseyin "İzin ver" demeden Ahmet giremez** —
kod doğru olsa bile.

![Hüseyin'in ekranında katılım isteği](images/ag.png)

### 5. Dosyaları indir

**Dosyaları indir** → projenin ineceği yeri seç (içine yeni bir klasör açılır). İlerleme
çubuğu gerçek dosya boyutunu gösterir; bağlantı koparsa tekrar bastığında kaldığı yerden
devam eder. Bitince pencere kendiliğinden Dosyalar sayfasına geçer.

![İndirme sürüyor](images/adim4-indirme.png)

---

## Birlikte çalışmak

### 6. Kilitle → düzenle → kaydet → gönder

1. Üzerinde çalışacağın dosyayı seç, **Kilitle**'ye bas. Artık o dosya sende; kilitlemediğin
   dosyalar salt-okunur olur.
2. SolidWorks'te düzenle ve kaydet. Satır **Değişti** olur.
3. Alttaki kutuya ne yaptığını yaz, **Değişiklikleri kaydet**. Kilit kendiliğinden kalkar.
4. Ahmet isen: **Ağ** → **Kendi değişikliklerimi gönder**.

Kilit herkesin ekranında anında görünür — aşağıda Hüseyin, Ahmet'in gövdeyi tuttuğunu görüyor:

![Hüseyin'in ekranında "Ahmet kilitledi"](images/adim5-kilit.png)

### 7. Gelen değişikliği al

Biri bir şey gönderdiğinde dosyaların **kendiliğinden değişmez** — SolidWorks'te açık bir
montajın altından parça çekilmesin diye. Bunun yerine **⬇ 1 yeni değişiklik — al** belirir;
hazır olunca bas.

![Gelen değişiklik bekliyor](images/adim6-al.png)

Ahmet tarafında aynı iş **Ağ → Yeni değişiklikleri al** ile yapılır. Almak yalnızca değişen
dosyalara dokunur; kaydetmediğin bir düzenlemenin üzerine yazacaksa durur ve önce kaydetmeni
ister.

Arada başka biri **farklı** bir dosyayı kaydetmişse göndermek yine çalışır: senin değişikliğin
onunkinin üzerine eklenir, kimseye bir şey sorulmaz.

### 8. Eski bir sürüme dön

**Geçmiş** → kaydı seç → alttan dosyayı seç → **Bu sürümü geri yükle**. Dosya o hâline döner ve
**Değişti** görünür; kaydedince geçmişe yeni bir kayıt olarak eklenir. Hiçbir şey silinmez.

![Geçmiş ve geri yükleme](images/gecmis.png)

### 9. Ayrı çalıştıysanız

İkiniz de **aynı** parçayı değiştirdiyseniz (örneğin biriniz oturum dışındayken), göndermeye
çalışınca **Ayrılığı çöz…** belirir. Yalnızca gerçekten çakışan dosyalar sorulur; her biri için hangisinin
kalacağını seç. Karşı taraf aynı seçimleri onaylamadan hiçbir dosya değişmez, seçilmeyen sürüm
de geçmişte kalır.

![Birleştirme ekranı](images/birlestirme.png)

---

## Bir şey ters giderse

| Belirti | Bak |
|---|---|
| Oturum listede görünmüyor | Aynı ağda mısınız? Hüseyin'in bilgisayarında Güvenlik Duvarı izni verildi mi? |
| "Farklı sürüm" uyarısı | Herkes aynı zip'ten kurmalı. |
| "Daha yeni sürümü var, önce güncelle" | Önce **al**, sonra kilitle. |
| "Başkasında kilitli" | O kişiye sor; oturumu açan kişi gerekirse **Kilidi kır** diyebilir. |
| Anlamadığın bir hata | Sol menü → **Kullanım kaydı** → klasördeki son `.jsonl` dosyası ne olduğunu gösterir. |

## Terimler

| Uygulamada | Anlamı | Git'teki karşılığı |
|---|---|---|
| Kayıt | Klasörün o anki hâlinin fotoğrafı | commit |
| Kilit | "Bu dosya şu an bende, başkası değiştiremez" | (Git LFS lock) |
| Oturum | Birinin projesini ağda paylaştığı süre | — |
| Katılım kodu | Oturuma girmek için gereken tek seferlik kod | — |
| Al | Gelen değişiklikleri kendi dosyalarına uygulamak | pull |
| Gönder | Kendi kayıtlarını oturuma iletmek | push |
| Ayrılık / birleştirme | İkiniz de ayrı ayrı çalıştıysanız hangi sürümün kalacağına karar vermek | merge (ama otomatik değil) |
| Salt-okunur | Diskte değiştirilemeyen dosya; SolidWorks onu salt-okunur açar | — |
