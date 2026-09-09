# Başka bilgisayardan devam

ÖNCE [güncel kabul panosunu](TASK-STATUS.md) ve [2026-09-10 checkpoint'i](../reports/checkpoints/2026-09-10/README.md) oku. Sonraki tek iş dış AI'ın Görev1 revizyonunu son üç QC bulgusuna karşı kontrol etmek. Aşağıdaki “Görev1 henüz yok” satırları tarihsel; artık kaynak61101f2'de1–14 teslimleri bulunuyor, fakat kabul durumları farklı.

```sh
git clone https://github.com/CeyhanHans/deribitproje-.git
cd deribitproje-
python --version
python -m unittest discover -s tests -v
```

Python 3.11 veya üzeri kullan. Windows'ta gerekirse python yerine py -3.11. Mevcut çekirdek/testler standart kütüphane kullanır; API key gerekmez. API indirme ağ bağlantısı ister; bu unit test komutu ağ gerektirmez.

Okuma sırası: AGENTS.md → docs/STATUS.md → docs/DECISIONS.md → orchestration/tasks/0_OKU_ONCE.txt → atanmış N.txt.
TXT içindeki eski Windows mutlak yollarını bu clone köküne uyarlayın. Görev dosyaları orchestration/tasks altındadır. ea62db2 eski başlangıç commit'idir; yeni çalışmada git rev-parse HEAD ile güncel baseline kaydedin.

Şimdi yapılacak: 1.txt ortak sözleşmeleri oluşturur. core/contracts.py ve specs/contracts-v1.md henüz mevcut değildir. 24 görevin hazır olması kodlandığı anlamına gelmez.

Dalgalar: 1 → (2,3,4,5,6,8) → (7,9,10,11) → (12,14) → (13,15) → 16 → (17,18) → 19 → 20 → (21,23) → 22 → 24.
Her ajan kendi branch'inde onaylı bağımlılıkların üstünde çalışır. Ana orkestratöre değişiklik patch'i, base/dependency hash'leri ve test kanıtı teslim edilir. Ortak contract dosyaları Görev1 kabulünden sonra salt okunur.

Henüz kullanıcı stratejisi çalıştıran genel CLI yok: python -m app.cli gelecekteki Görev16 hedefidir; bugün çalışan komut diye sunmayın. Tam Mart2024-Mayıs2025 raporu henüz üretilmedi.

Bu depo proje için taşınabilir kaynaktır. Obsidian veya Mem0 erişimi devam etmek için gerekmez. Önceki genel yedek CeyhanHans/butunprojeyedekleri commit 12fe038; yalnız proje bağlamı bu depoya aktarıldı. Kişisel ikinci beynin diğer projeleri aktarılmadı.
