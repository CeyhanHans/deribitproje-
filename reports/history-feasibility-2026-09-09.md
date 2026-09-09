# Deribit old option history feasibility (corrected host comparison)

Date: 2026-09-09

## Scope and correction

This was a bounded, read-only feasibility check. No API key was used, `count` never exceeded 5, no bulk data was downloaded, and no raw responses were saved.

The first pass mistakenly tested `https://www.deribit.com/api/v2/public`. The previously successful control used `https://history.deribit.com/api/v2/public`. This correction preserves the live-host result for comparison and adds exactly 12 calls on the correct history host.

Classifications:

- `VALID_DATA`: at least one trade was returned.
- `ACCEPTED_ZERO_TRADES`: normal result, empty `trades`, `has_more=false`.
- `INVALID_NAME`: HTTP 400 / JSON-RPC `-32602` tied to `instrument_name`.

A blank result is not labelled a retention limit.

## A. Live production host comparison

The original 15-call pass used the live production host. Eight 2024/2025 candidate names returned `ACCEPTED_ZERO_TRADES` in lifetime-oriented time queries; six representative sequence cross-checks also returned zero. The deliberately invented `BTC-27SEP24-123456-C` returned `instrument not found`.

| Instrument | Time result | Sequence result where checked |
|---|---:|---:|
| BTC-27SEP24-60000-C | ACCEPTED_ZERO_TRADES | ACCEPTED_ZERO_TRADES |
| BTC-27SEP24-60000-P | ACCEPTED_ZERO_TRADES | ACCEPTED_ZERO_TRADES |
| BTC-27DEC24-100000-C | ACCEPTED_ZERO_TRADES | ACCEPTED_ZERO_TRADES |
| BTC-27DEC24-100000-P | ACCEPTED_ZERO_TRADES | not checked |
| BTC-28MAR25-100000-C | ACCEPTED_ZERO_TRADES | not checked |
| BTC-28MAR25-100000-P | ACCEPTED_ZERO_TRADES | ACCEPTED_ZERO_TRADES |
| BTC-27JUN25-100000-C | ACCEPTED_ZERO_TRADES | ACCEPTED_ZERO_TRADES |
| BTC-27JUN25-100000-P | ACCEPTED_ZERO_TRADES | ACCEPTED_ZERO_TRADES |
| BTC-27SEP24-123456-C | INSTRUMENT_NOT_FOUND | not checked |

Exact live-host time URLs:

1. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27SEP24-60000-C&start_timestamp=1711872000000&end_timestamp=1727424000001&count=5&sorting=asc`
2. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27SEP24-60000-P&start_timestamp=1711872000000&end_timestamp=1727424000001&count=5&sorting=asc`
3. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27DEC24-100000-C&start_timestamp=1719734400000&end_timestamp=1735286400001&count=5&sorting=asc`
4. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27DEC24-100000-P&start_timestamp=1719734400000&end_timestamp=1735286400001&count=5&sorting=asc`
5. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-28MAR25-100000-C&start_timestamp=1727596800000&end_timestamp=1743148800001&count=5&sorting=asc`
6. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-28MAR25-100000-P&start_timestamp=1727596800000&end_timestamp=1743148800001&count=5&sorting=asc`
7. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27JUN25-100000-C&start_timestamp=1735459200000&end_timestamp=1751011200001&count=5&sorting=asc`
8. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27JUN25-100000-P&start_timestamp=1735459200000&end_timestamp=1751011200001&count=5&sorting=asc`
9. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27SEP24-123456-C&start_timestamp=1711872000000&end_timestamp=1727424000001&count=5&sorting=asc`

Exact live-host sequence URLs:

10. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27JUN25-100000-C&start_seq=1&end_seq=5&count=5&sorting=asc`
11. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27JUN25-100000-P&start_seq=1&end_seq=5&count=5&sorting=asc`
12. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27SEP24-60000-C&start_seq=1&end_seq=5&count=5&sorting=asc`
13. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27SEP24-60000-P&start_seq=1&end_seq=5&count=5&sorting=asc`
14. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27DEC24-100000-C&start_seq=1&end_seq=5&count=5&sorting=asc`
15. `https://www.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-28MAR25-100000-P&start_seq=1&end_seq=5&count=5&sorting=asc`

These live-host results say nothing about availability on the history host.

## B. Correct history host

### Sequence checks (9 calls)

Each real candidate used `start_seq=1&end_seq=5&count=5&sorting=asc`. All eight returned five historical trades.

| Instrument | Result | Sequence values | First returned UTC | Last returned UTC |
|---|---:|---|---|---|
| BTC-27SEP24-60000-C | VALID_DATA (5) | 1,2,3,4,5 | 2023-09-28 20:15:23.043 | 2023-09-29 09:17:49.824 |
| BTC-27SEP24-60000-P | VALID_DATA (5) | 1,2,3,4,5 | 2023-10-23 23:34:56.981 | 2023-11-09 07:13:14.967 |
| BTC-27DEC24-100000-C | VALID_DATA (5) | 1,2,3,4,5 | 2023-12-29 03:10:06.799 | 2023-12-29 13:08:20.975 |
| BTC-27DEC24-100000-P | VALID_DATA (5) | 1,2,3,4,5 | 2024-01-12 16:39:01.394 | 2024-01-17 10:04:14.917 |
| BTC-28MAR25-100000-C | VALID_DATA (5) | 1,2,3,5,4 | 2024-03-28 10:01:33.913 | 2024-03-28 11:39:26.240 |
| BTC-28MAR25-100000-P | VALID_DATA (5) | 1,2,3,4,5 | 2024-03-28 20:47:57.906 | 2024-04-05 13:04:00.515 |
| BTC-27JUN25-100000-C | VALID_DATA (5) | 1,2,3,4,5 | 2024-06-27 15:35:54.105 | 2024-06-28 01:21:21.697 |
| BTC-27JUN25-100000-P | VALID_DATA (5) | 1,2,3,4,5 | 2024-07-15 22:37:19.269 | 2024-07-26 14:04:02.096 |
| BTC-27SEP24-123456-C | INVALID_NAME | n/a | n/a | n/a |

The invented name returned HTTP 400 / JSON-RPC `-32602`, `reason="wrong format"`. One valid response delivered rows as `1,2,3,5,4`, so consumers must sort and deduplicate by sequence rather than trust response order.

Exact history-host sequence URLs:

1. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27SEP24-60000-C&start_seq=1&end_seq=5&count=5&sorting=asc`
2. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27SEP24-60000-P&start_seq=1&end_seq=5&count=5&sorting=asc`
3. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27DEC24-100000-C&start_seq=1&end_seq=5&count=5&sorting=asc`
4. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27DEC24-100000-P&start_seq=1&end_seq=5&count=5&sorting=asc`
5. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-28MAR25-100000-C&start_seq=1&end_seq=5&count=5&sorting=asc`
6. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-28MAR25-100000-P&start_seq=1&end_seq=5&count=5&sorting=asc`
7. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27JUN25-100000-C&start_seq=1&end_seq=5&count=5&sorting=asc`
8. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27JUN25-100000-P&start_seq=1&end_seq=5&count=5&sorting=asc`
9. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument?instrument_name=BTC-27SEP24-123456-C&start_seq=1&end_seq=5&count=5&sorting=asc`

### Lifetime-spanning time checks (3 calls)

The windows begin shortly before the first returned trade and end one millisecond after scheduled expiry. Each returned five earliest trades and `has_more=true`, proving more rows exist beyond this deliberately bounded page.

| Instrument | Requested window UTC | Result | First returned UTC | Last returned UTC | has_more |
|---|---|---:|---|---|---:|
| BTC-27SEP24-60000-C | 2023-09-01 to 2024-09-27 08:00:00.001 | VALID_DATA (5) | 2023-09-28 20:15:23.043 | 2023-09-29 09:17:49.824 | true |
| BTC-27DEC24-100000-C | 2023-12-01 to 2024-12-27 08:00:00.001 | VALID_DATA (5) | 2023-12-29 03:10:06.799 | 2023-12-29 13:08:20.975 | true |
| BTC-27JUN25-100000-C | 2024-06-01 to 2025-06-27 08:00:00.001 | VALID_DATA (5) | 2024-06-27 15:35:54.105 | 2024-06-28 01:21:21.697 | true |

Exact history-host time URLs:

10. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27SEP24-60000-C&start_timestamp=1693526400000&end_timestamp=1727424000001&count=5&sorting=asc`
11. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27DEC24-100000-C&start_timestamp=1701388800000&end_timestamp=1735286400001&count=5&sorting=asc`
12. `https://history.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time?instrument_name=BTC-27JUN25-100000-C&start_timestamp=1717200000000&end_timestamp=1751011200001&count=5&sorting=asc`

## Corrected conclusions

1. **Old historical trade retrieval: PROVEN for the sample.** The correct history host returned Call and Put trade rows for candidates expiring in September 2024, December 2024, March 2025, and June 2025. The `BTC-27JUN25-100000-C` control was reproduced.
2. **Host selection is mandatory.** The live host returned empty results for these old instruments; the history host returned data.
3. **Deterministic candidate verification: FEASIBLE but PARTIAL.** Generated names can be probed and the deliberately invented strike was rejected.
4. **Complete 360-day universe: NOT PROVEN.** Successful probing does not reconstruct every strike ever listed, exact listing timestamp, schedule exception, or listed instrument with zero trades. A generated catalog must be called a `trade-observed/discovered universe`, not a complete official universe, unless independently audited.
5. **Individual contract date coverage: PARTIAL but strong.** Lifetime-spanning calls returned data with `has_more=true`; exhaustive pagination and completeness checks were intentionally outside this test.

## Safe next step

Historical option-trade collection must explicitly use `https://history.deribit.com/api/v2/public`. Add a regression test preventing accidental use of the live host for old-history collection. Before any 30-360 day download, define a bounded discovery policy and report missing-name and zero-trade counters. Do not label the resulting catalog complete.
