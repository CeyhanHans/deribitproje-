# Minimum Veri Sözlüğü

Her kayıt için saklanacak alanlar:

```text
timestamp_utc, instrument_name, expiry_utc, strike, option_type,
contract_size, settlement_currency, underlying_price,
best_bid, best_ask, mark_price,
bid_iv, ask_iv, mark_iv,
delta, gamma, vega, theta,
open_interest, volume
```

İşlem simülasyonuna ek alanlar:

```text
strategy_id, side, contracts, entry_price, exit_price,
fees, spread_cost, slippage_assumption,
realized_pnl, unrealized_pnl, data_quality_label
```

`data_quality_label`: `live-collected`, `official-historical`, `third-party-L2` veya `estimated`.
