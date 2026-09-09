# Kararlar ve gerekçeleri

1. Kullanıcı hedefi: tarih aralığı ve X stratejisini seçip gerçek piyasa verisinde kazanma/kaybetme oranı, net PnL ve risk ölçmek. Mart2024-Mayıs2025 ve straddle örnektir. 30/60/90/120/360 gün kısayolları ürün sınırı değildir.
2. İlk kapsam BTC inverse, 1h/1d karar çözünürlüğü. USD-settled ve hedge/funding ürünleri ayrı muhasebe ve kabul ister; şema enum'u tek başına destek değildir.
3. History işlemler için history.deribit.com; canlı snapshot için www.deribit.com. Eski verinin canlı hostta boş dönmesi geçmişin yokluğunu kanıtlamaz.
4. Trade fiyatı gerçek işlemdir; aynı anda bizim miktarımız için executable quote kanıtı değildir. Fill ve veri kalite etiketleri ayrı. Mevcut model fiyat üzerinden slippage; gerçek bid/ask replay ileri katmandır.
5. Giriş/çıkış maliyetleri ilk sürümde var. Temel/iyimser/kötümser senaryolar açık parametredir; kalibre edilmediyse varsayım diye yazılır. Geçmişteki veya gelecekteki kesin kârlılık garantisi verilmez.
6. listing<=T<expiry ve available_at<=decision_time. Gelecek expiry tek başına ret sebebi değildir. Açık pozisyonun strike'ı spot değiştiğinde sessizce değişmez.
7. Sinyal tamamlanmış bar üzerinden; emir daha sonraki uygun observation'da. Aynı bar kapanışıyla geriye fill yazmak yasak. Saatlik bacak fiyatları eşzamanlı olmayabilir.
8. Eksik trade barı bilinmeyen gözlemdir. Açık pozisyonu veya gerçekleşmemiş zararı silmek yasak. İndirme eksikliği ile işlemsiz saat ayrı raporlanır; %85 pilot hedefi kanıtlanmış gerçek değildir.
9. BTC defteri esas; USD equity, BTC benchmark ve trade PnL ayrı. Settlement resmi delivery fiyatıyla; perpetual proxy yerine geçirilemez. Fee geçmiş yürürlük kanıtı yoksa configured_unverified.
10. Short stratejiler için sermaye/risk modeli zorunlu. V1 stress reserve gerçek exchange margin eşdeğeri değildir; ileri görev23 resmi margin.
11. Şimdilik alt ajan yok. Kullanıcı TXT'leri başka ajanlara dağıtır, ana Codex sözleşme/QC/birleştirme yapar. Önceki dış ortakla peer review düzeni bu aşamada kullanılmıyor.
12. Görevler dalgalar halinde, tek dosya sahibiyle ilerler. Önkoşul kabul edilmeden sonraki dalga başlamaz. Son QC toplu olsa da ara sözleşme entegrasyonu gerekir.
13. Başlangıç veri bütçesi $0, önceki gerekçeli üst sınır $3–5. Ücretli veri satın alma ayrıca açık kullanıcı kararı gerektirir.

Tasarım girdisi docs/references/chatgpt_icin_cevap7.txt arşivlenmiştir. İçindeki sıfır-bias, O(1) ve likidite ifadeleri bağımsız kanıt değildir; düzeltmeler görev paketinde açıklanır.
