# Deribit BTC Inverse Option Fee Model

Araştırma tarihi: 2026-09-09  
Kapsam: BTC inverse options için backtestte gereken trading fee, delivery fee, cap ve birim modellemesi.  
Kaynak sınırı: Yalnız Deribit resmi destek sayfaları kullanıldı.

## Resmi kaynaklar

- Deribit Support, `Fees`, updated 2026-08-17: https://support.deribit.com/hc/en-us/articles/25944746248989-Fees
- Deribit Support, `Inverse Options`, updated 2026-03-25: https://support.deribit.com/hc/en-us/articles/31424939096093-Inverse-Options
- Deribit Support, `Settlement`, updated 2026-04-16: https://support.deribit.com/hc/en-us/articles/29734325712413-Settlement

## Kaynakta açık olan noktalar

### Enstrüman ve birimler

- BTC inverse option tipi `Inverse` ve kategori `Option`.
- Underlying/ticker: `Deribit BTC Index`.
- Quoted currency: `BTC`; orderbook USD karşılığı BTC index ile gösterilir.
- Margin currency: `BTC`.
- Settlement method: cash settlement in `BTC`.
- Contract multiplier / contract size: `1 BTC`; her sözleşme 1 BTC temsil eder, premium 1 BTC için gösterilir.
- Minimum order size: `0.1` option contract.
- Delivery price: Deribit index'in 07:30-08:00 UTC arasında ölçülen TWAP değeri.
- Expiry/exercise: European, cash-settled, automatic exercise at expiry.
- Exercise settlement value: USD intrinsic amount delivery price ile hesaplanır; underlying asset amount bu farkın settlement value'ya bölünmesiyle bulunur. BTC için bu, ITM call/put settlement amount'un BTC cinsinden yazılması demektir.

### Trading fee

- Deribit maker-taker fee modeli kullanır.
- Ücretler ürün bazında değişir ve sözleşmenin underlying asset'inin yüzdesi olarak hesaplanır.
- `Maker` order likidite ekleyen, `Taker` order likidite kaldıran emirdir.
- Standart fee tier için options maker/taker: `3 / 3 bps`.
- `1 bp = 0.01%`; bu nedenle standart options maker/taker oranı `0.03% / 0.03%`.
- Limit order tek başına maker olmayı garanti etmez; maker garanti etmek için iceberg olmaması ve `Post` etkin olması gerekir.
- Iceberg order'da görünen kısım maker olabilir; hidden kısım her zaman taker fee alır.

### Delivery fee

- Futures ve options delivery işlemleri expiry'de ek ücret alabilir.
- Daily options delivery fee'den muaftır: `BTC/ETH Daily Options = 0%`.
- BTC/ETH options delivery fee: `0.015%`; bu ücret option value'nun `12.5%` seviyesinden yüksek olamaz.
- Delivery fee, final expiry'de uygulanır; daily settlement sırasında uygulanmaz.
- Daily options ve weekly futures genelde/explisit olarak delivery fee'den muaftır.

### Fee cap

- Options trade veya delivery fee'leri option value'nun `12.5%` seviyesini aşamaz.
- Fees tablosunda standart tier için options fee cap `12.5% of premium` olarak gösterilir.
- VIP seviyelerinde effective fee cap daha düşük gösterilir; örnekler:
  - Standard: `12.5`
  - VIP1: `10.42`
  - VIP2: `8.33`
  - VIP3: `7.29`
  - VIP4: `6.25`
  - VIP5/VIP6: `4.17`
  - VIP7: `4.17`
- Cap uygulanmış fee, sonra kullanıcının uygulanabilir volume-based discount'u ile ayrıca azaltılır.

### İndirimler ve hesap seviyesine bağlı alanlar

- Fee tier otomatik belirlenir; kriterler equity veya son 30 gün USD-equivalent notional volume olabilir.
- VIP tier, options ve futures tarafında ayrı volume eşikleriyle kazanılabilir fakat ulaşılan en yüksek VIP seviyesi her iki product type fee'sine uygulanabilir.
- Fee discount seviyesi account settings'ten veya `private/get_account_summary` / `private/get_account_summaries` çağrılarında `extended:true` ile görülebilir.
- Fee discounts regular trading fees'e uygulanır; delivery fee ve liquidation fee'ye uygulanmaz.
- Ancak options fee cap bölümünde cap'in volume-based discount ile azaltıldığı belirtilir. Bu yüzden backtestte "regular trade fee discount" ve "effective cap discount" ayrı alanlar olarak tutulmalı.
- VIP7 notunda kendi hesabına trading için geçerli olduğu, broker olarak kullanılamadığı ve options settlement fees için full waiver bulunduğu belirtilir.
- Fee Balance varsa aynı settlement currency'deki eligible trading, liquidation ve delivery fee'leri ödemede kullanılabilir; collateral/funding gibi amaçlarda kullanılamaz.

## Backtest model alanları

Bu alanlar BTC inverse option fill ve expiry event'lerini modellemek için yeterli minimum şemadır.

### Instrument metadata

```yaml
instrument_type: option
option_style: european
option_settlement: cash
inverse: true
underlying_index: Deribit BTC Index
quoted_currency: BTC
margin_currency: BTC
settlement_currency: BTC
contract_multiplier_btc: 1
contract_size_btc: 1
minimum_order_size_contracts: 0.1
delivery_price_window_utc: 07:30-08:00
expiry_time_utc: 08:00
```

### Trade fee event

```yaml
event: trade
liquidity_role: maker | taker
is_daily_option: true | false
fee_tier: Standard | VIP1 | VIP2 | VIP3 | VIP4 | VIP5 | VIP6 | VIP7 | account_specific
option_trade_fee_bps_maker: account_specific
option_trade_fee_bps_taker: account_specific
standard_option_trade_fee_bps_maker: 3
standard_option_trade_fee_bps_taker: 3
trade_fee_currency: BTC
trade_fee_contract_basis: underlying_asset_percent
trade_fee_cap_basis: premium_or_option_value_btc
trade_fee_cap_percent_of_premium_standard: 12.5
trade_fee_cap_percent_effective: account_specific
```

Standart, tek bacaklı, non-block BTC inverse option trade için uygulanabilir model:

```text
base_fee_btc = amount_contracts * min(
  option_trade_fee_rate * 1 BTC,
  trade_fee_cap_percent_effective * option_premium_btc_per_contract
)
```

Not: Bu formül, Deribit'in "underlying asset yüzdesi", BTC option contract size `1 BTC`, options fee bps ve premium cap ifadelerinin backtest modeline çevrilmiş halidir. Kaynak sayfa regular trade fee için tek satırlı formül vermiyor; kesin uygulamada account summary/API fee tier doğrulanmalıdır.

### Delivery fee event

```yaml
event: expiry_delivery
is_daily_option: true | false
is_itm: true | false
delivery_fee_rate_standard: 0.00015
delivery_fee_percent_standard: 0.015
delivery_fee_currency: BTC
delivery_fee_cap_percent: 12.5
delivery_fee_discount_applies: false
vip7_options_settlement_fee_waiver: account_specific
delivery_fee_cap_basis: option_value_at_delivery_btc
```

Backtestte daily olmayan BTC inverse options için önerilen korumalı model:

```text
if is_daily_option:
  delivery_fee_btc = 0
else:
  delivery_fee_btc = min(
    0.00015 BTC * delivered_or_settled_contract_amount,
    12.5% * option_value_at_delivery_btc
  )
```

Bu delivery formülünün iki alanı kaynakta kesinleşmiyor:

- `delivered_or_settled_contract_amount`: Delivery fee'nin net ITM settlement amount'a mı, gross expiring position size'a mı, yoksa başka bir exchange-internal amount'a mı uygulandığı Fees/Inverse Options/Settlement sayfalarında BTC inverse için açık formül olarak verilmemiştir.
- `option_value_at_delivery_btc`: Fees sayfası delivery cap için "option's value" der; trading fee tablosunda cap "premium" olarak geçer. Expiry'de premium yerine intrinsic settlement value kullanılıp kullanılmadığı bu kaynak setinde açık formülle verilmemiştir.

Bu nedenle delivery fee backtest hesaplamasında kesin hesap oranı üretmek yerine şu alanlar parametrik tutulmalı:

```yaml
delivery_fee_amount_basis:
  allowed_values:
    - gross_expiring_contract_amount
    - itm_settlement_amount_btc
    - exchange_reported_delivery_amount
  required_source: account_statement_or_trade_history
delivery_fee_cap_value_basis:
  allowed_values:
    - expiry_intrinsic_value_btc
    - option_mark_or_settlement_value_btc
    - exchange_reported_option_value
  required_source: account_statement_or_delivery_record
```

## Verilemeyen veya hesap seviyesine bağlı noktalar

- Kullanıcının gerçek maker/taker oranı Standard olmayabilir. VIP tier, affiliate/volume rules, özel indirimler ve account settings/API sonucu gerekir.
- VIP7 için options settlement fee waiver hesabın uygunluğuna bağlıdır; genel backtest default'u yapılamaz.
- Combo ve block trade işlemlerinde fee modeli ayrı indirimlere sahiptir. Bu dosya tek bacak normal order modelini kapsar; combo/block fill backtestleri ayrı event type gerektirir.
- Fee Balance, gerçek nakit bakiyeden fee düşümünü değiştirebilir. PnL backtestinde ekonomik fee maliyeti ayrı, cash balance fee debit ayrı izlenmelidir.
- Delivery fee'nin BTC inverse options için exact `amount` ve exact cap tabanı bu kaynak setinde formül olarak verilmedi. Account statement veya Deribit API delivery/transaction kayıtlarıyla eşleştirme yapılmadan kesin hesap oranı tahmin edilmemelidir.
- Liquidation fees bu dosyanın kapsamı değildir. Fees sayfasında BTC options liquidation için ayrı `MIN(0.01, 0.25 * OptionPrice) * Amount` formülü bulunur, ancak normal trade/delivery backtestine karıştırılmamalıdır.

## Uygulama kararı

Backtestte varsayılan olarak:

1. Normal trade fee için account tier yoksa `Standard` kullan, fakat sonucu `standard_fee_estimate` olarak işaretle.
2. Maker/taker role fill seviyesinde saklanmalı; limit order maker varsayılmamalı.
3. BTC inverse option premium, fee ve settlement cashflow'ları BTC cinsinden izlenmeli; USD yalnız strike/index/delivery price ve raporlama karşılığı için kullanılmalı.
4. Daily options için delivery fee `0` olmalı.
5. Daily olmayan expiry delivery için fee parametreleri saklanmalı, fakat exact fee reconciliation yapılmadan kesin delivery fee debit'i tahmin edilmemeli.
