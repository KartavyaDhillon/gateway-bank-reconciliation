"""
generate_data.py — creates realistic DUMMY data for the reconciliation demo.

Produces two files for one business day:
  data/gateway_settlement_<date>.csv  — what the payment gateway says it settled
  data/bank_statement_<date>.csv      — what actually credited in the bank

Deliberately injects real-world problems so the reconciliation has something to catch:
  * transactions settled by the gateway but never credited in the bank
  * bank credits with no matching gateway record
  * amount mismatches (short / excess credit)
  * credits that land a day late (should still match, with a note)
  * duplicate bank credits
  * UTR missing from the bank narration (forces fallback matching)
All names, IDs and amounts are synthetic.
"""
import argparse
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

SEED = 42
NARRATION_TEMPLATES = [
    "NEFT CR-{utr}-PAYGW SETTLEMENT",
    "IMPS/{utr}/GATEWAY PAYOUT",
    "RTGS-{utr}-PG SETTL",
    "UPI SETTLEMENT {utr}",
]


def make_utr(rng: random.Random) -> str:
    return "UTR" + "".join(rng.choice("0123456789") for _ in range(12))


def build(day: date, n: int, rng: random.Random):
    gateway_rows, bank_rows = [], []
    for i in range(1, n + 1):
        amount = round(rng.choice([rng.uniform(100, 5000), rng.uniform(5000, 50000), rng.uniform(50000, 250000)]), 2)
        fee = round(amount * 0.018, 2)
        net = round(amount - fee, 2)
        utr = make_utr(rng)
        txn_time = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rng.randint(0, 86399))
        gateway_rows.append({
            "txn_id": f"PG{day:%y%m%d}{i:06d}",
            "order_id": f"ORD-{rng.randint(100000, 999999)}",
            "txn_time": txn_time.strftime("%Y-%m-%d %H:%M:%S"),
            "settlement_date": day.isoformat(),
            "gross_amount": amount,
            "gateway_fee": fee,
            "net_amount": net,
            "utr": utr,
        })
        bank_rows.append({
            "value_date": day.isoformat(),
            "narration": rng.choice(NARRATION_TEMPLATES).format(utr=utr),
            "ref_no": f"BNK{rng.randint(10**9, 10**10 - 1)}",
            "credit_amount": net,
        })

    idx = list(range(n))
    rng.shuffle(idx)
    cursor = 0

    def take(k):
        nonlocal cursor
        out = idx[cursor:cursor + k]
        cursor += k
        return out

    missing_in_bank = take(14)
    amount_mismatch = take(11)
    late_credit = take(9)
    no_utr = take(10)
    duplicates = take(3)

    for i in amount_mismatch:
        base = bank_rows[i]["credit_amount"]
        delta = rng.choice([-1, 1]) * round(base * rng.uniform(0.002, 0.03) + rng.uniform(1, 50), 2)
        bank_rows[i]["credit_amount"] = round(bank_rows[i]["credit_amount"] + delta, 2)
    for i in late_credit:
        bank_rows[i]["value_date"] = (day + timedelta(days=1)).isoformat()
    for i in no_utr:
        bank_rows[i]["narration"] = "NEFT CR-PAYMENT GATEWAY SETTLEMENT"
    dup_rows = [dict(bank_rows[i], ref_no=f"BNK{rng.randint(10**9, 10**10 - 1)}") for i in duplicates]

    bank_rows = [r for j, r in enumerate(bank_rows) if j not in set(missing_in_bank)] + dup_rows

    # bank credits that have no gateway record at all
    for _ in range(7):
        bank_rows.append({
            "value_date": day.isoformat(),
            "narration": rng.choice(NARRATION_TEMPLATES).format(utr=make_utr(rng)),
            "ref_no": f"BNK{rng.randint(10**9, 10**10 - 1)}",
            "credit_amount": round(rng.uniform(500, 60000), 2),
        })

    rng.shuffle(bank_rows)
    return pd.DataFrame(gateway_rows), pd.DataFrame(bank_rows)


def main():
    ap = argparse.ArgumentParser(description="Generate dummy gateway + bank files")
    ap.add_argument("--date", default="2026-09-30", help="business date YYYY-MM-DD")
    ap.add_argument("--rows", type=int, default=1250, help="number of gateway transactions")
    ap.add_argument("--out", default="data", help="output folder")
    args = ap.parse_args()

    day = date.fromisoformat(args.date)
    gw, bank = build(day, args.rows, random.Random(SEED))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    gw.to_csv(out / f"gateway_settlement_{day:%Y%m%d}.csv", index=False)
    bank.to_csv(out / f"bank_statement_{day:%Y%m%d}.csv", index=False)
    print(f"Gateway rows: {len(gw):,} | Bank rows: {len(bank):,} -> {out.resolve()}")


if __name__ == "__main__":
    main()
