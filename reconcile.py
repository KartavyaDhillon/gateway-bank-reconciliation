"""
reconcile.py — Payment Gateway vs Bank Statement reconciliation.

Usage:
    python reconcile.py --gateway data/gateway_settlement_20260930.csv \
                        --bank data/bank_statement_20260930.csv \
                        --out output

Matching rules (in order):
  1. Duplicate bank credits (same UTR + amount seen twice)      -> "Duplicate Credit"
  2. Match on UTR extracted from the bank narration
       - amounts equal (within tolerance)                       -> "Matched"
       - credited 1 day late                                    -> "Matched (T+1 credit)"
       - amounts differ                                         -> "Amount Mismatch"
  3. Fallback for narrations without a UTR: unique match on
     net amount within +/- DATE_WINDOW days                     -> "Matched (amount + date)"
  4. Left over gateway rows                                     -> "Missing in Bank"
     Left over bank rows                                        -> "Unknown Credit"

Outputs:
  output/Reconciliation_<date>.xlsx   colour-coded, one sheet per bucket + Summary
  output/summary_email_<date>.html    the summary email body
Email is sent only if SMTP_HOST / SMTP_USER / SMTP_PASS / MAIL_TO are set.
"""
import argparse
import logging
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

import pandas as pd

from env_loader import load_env

load_env()
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

AMOUNT_TOLERANCE = 0.01
DATE_WINDOW = 1
UTR_PATTERN = re.compile(r"UTR\d{12}")

COLORS = {
    "Matched": "E2F0D9",
    "Matched (T+1 credit)": "E2F0D9",
    "Matched (amount + date)": "E2F0D9",
    "Amount Mismatch": "FFF2CC",
    "Missing in Bank": "F8CBAD",
    "Unknown Credit": "FCE4D6",
    "Duplicate Credit": "DDEBF7",
}
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
THIN = Side(style="thin", color="BFBFBF")

log = logging.getLogger("recon")


# ---------------------------------------------------------------- load
def load(gateway_path: Path, bank_path: Path):
    gw = pd.read_csv(gateway_path, dtype={"utr": str})
    bank = pd.read_csv(bank_path)
    for col in ("txn_id", "net_amount", "utr", "settlement_date"):
        if col not in gw.columns:
            raise ValueError(f"Gateway file missing column: {col}")
    for col in ("value_date", "narration", "credit_amount"):
        if col not in bank.columns:
            raise ValueError(f"Bank file missing column: {col}")
    gw["settlement_date"] = pd.to_datetime(gw["settlement_date"])
    bank["value_date"] = pd.to_datetime(bank["value_date"])
    bank["utr"] = bank["narration"].str.extract(f"({UTR_PATTERN.pattern})", expand=False)
    bank["bank_row"] = range(len(bank))
    return gw, bank


# ---------------------------------------------------------------- reconcile
def reconcile(gw: pd.DataFrame, bank: pd.DataFrame) -> pd.DataFrame:
    results = []
    used_bank, used_gw = set(), set()

    # 1. duplicates in bank
    dup_mask = bank.duplicated(subset=["utr", "credit_amount"], keep="first") & bank["utr"].notna()
    for _, b in bank[dup_mask].iterrows():
        results.append(_row("Duplicate Credit", None, b, "Same UTR and amount credited more than once"))
        used_bank.add(b.bank_row)

    # 2. UTR match
    bank_by_utr = bank[~bank.bank_row.isin(used_bank) & bank.utr.notna()].set_index("utr")
    for gi, g in gw.iterrows():
        if g.utr not in bank_by_utr.index:
            continue
        b = bank_by_utr.loc[g.utr]
        diff = round(b.credit_amount - g.net_amount, 2)
        day_gap = (b.value_date - g.settlement_date).days
        if abs(diff) <= AMOUNT_TOLERANCE:
            status = "Matched" if day_gap == 0 else "Matched (T+1 credit)"
            note = "" if day_gap == 0 else f"Credited {day_gap} day(s) after settlement"
        else:
            status = "Amount Mismatch"
            note = f"{'Short' if diff < 0 else 'Excess'} credit of {abs(diff):,.2f}"
        results.append(_row(status, g, b, note, diff))
        used_bank.add(b.bank_row)
        used_gw.add(gi)

    # 3. fallback: amount + date window for narrations without UTR
    open_bank = bank[~bank.bank_row.isin(used_bank) & bank.utr.isna()]
    open_gw = gw[~gw.index.isin(used_gw)]
    for _, b in open_bank.iterrows():
        cands = open_gw[
            ((open_gw.net_amount - b.credit_amount).abs() <= AMOUNT_TOLERANCE)
            & ((b.value_date - open_gw.settlement_date).dt.days.abs() <= DATE_WINDOW)
        ]
        if len(cands) == 1:
            gi = cands.index[0]
            results.append(_row("Matched (amount + date)", gw.loc[gi], b, "No UTR in narration - matched on amount and date"))
            used_bank.add(b.bank_row)
            used_gw.add(gi)
            open_gw = open_gw.drop(gi)

    # 4. leftovers
    for _, g in gw[~gw.index.isin(used_gw)].iterrows():
        results.append(_row("Missing in Bank", g, None, "Settled by gateway, not found in bank"))
    for _, b in bank[~bank.bank_row.isin(used_bank)].iterrows():
        results.append(_row("Unknown Credit", None, b, "Bank credit with no gateway record"))

    return pd.DataFrame(results)


def _row(status, g, b, note="", diff=None):
    return {
        "Status": status,
        "Txn ID": None if g is None else g.txn_id,
        "Order ID": None if g is None else g.order_id,
        "UTR": (g.utr if g is not None else b.utr),
        "Settlement Date": None if g is None else g.settlement_date.date(),
        "Gateway Net Amount": None if g is None else g.net_amount,
        "Bank Value Date": None if b is None else b.value_date.date(),
        "Bank Ref": None if b is None else b.ref_no,
        "Bank Credit": None if b is None else b.credit_amount,
        "Difference": diff if diff is not None else None,
        "Bank Narration": None if b is None else b.narration,
        "Remarks": note,
    }


# ---------------------------------------------------------------- summary
BUCKET_ORDER = ["Matched", "Matched (T+1 credit)", "Matched (amount + date)",
                "Amount Mismatch", "Missing in Bank", "Unknown Credit", "Duplicate Credit"]


def summarise(res: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for s in BUCKET_ORDER:
        part = res[res.Status == s]
        amt = part["Gateway Net Amount"].fillna(part["Bank Credit"]).sum()
        rows.append({"Status": s, "Count": len(part), "Amount": round(amt, 2)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- excel
def write_excel(res, summary, gw, bank, path: Path, biz_date: str):
    sheets = {
        "Exceptions": res[~res.Status.str.startswith("Matched")],
        "Amount Mismatch": res[res.Status == "Amount Mismatch"],
        "Missing in Bank": res[res.Status == "Missing in Bank"],
        "Unknown Credit": res[res.Status == "Unknown Credit"],
        "Duplicate Credit": res[res.Status == "Duplicate Credit"],
        "Matched": res[res.Status.str.startswith("Matched")],
        "All Results": res,
    }
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame().to_excel(xw, sheet_name="Summary")
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)

    wb = load_workbook(path)
    _build_summary_sheet(wb["Summary"], summary, gw, bank, biz_date)
    for name in sheets:
        _style_table(wb[name])
    wb.save(path)


def _build_summary_sheet(ws, summary, gw, bank, biz_date):
    ws.sheet_view.showGridLines = False
    ws["B2"] = "Payment Gateway vs Bank Reconciliation"
    ws["B2"].font = Font(size=16, bold=True, color="1F3864")
    ws["B3"] = f"Business date: {biz_date}"
    ws["B3"].font = Font(size=11, color="595959")

    matched = summary[summary.Status.str.startswith("Matched")].Count.sum()
    exceptions = summary[~summary.Status.str.startswith("Matched")].Count.sum()
    total_gw = len(gw)
    kpis = [
        ("Gateway txns", f"{total_gw:,}"),
        ("Bank credits", f"{len(bank):,}"),
        ("Match rate", f"{matched / total_gw:.1%}"),
        ("Exceptions", f"{exceptions:,}"),
    ]
    for i, (label, val) in enumerate(kpis):
        col = 2 + i * 2
        lc = ws.cell(row=5, column=col, value=label)
        vc = ws.cell(row=6, column=col, value=val)
        lc.font = Font(size=10, color="595959")
        vc.font = Font(size=18, bold=True, color="C00000" if label == "Exceptions" else "1F3864")

    hdr = ["Status", "Count", "Amount (INR)"]
    for j, h in enumerate(hdr):
        c = ws.cell(row=9, column=2 + j, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center")
    for i, r in enumerate(summary.itertuples(), start=10):
        vals = [r.Status, r.Count, r.Amount]
        for j, v in enumerate(vals):
            c = ws.cell(row=i, column=2 + j, value=v)
            c.fill = PatternFill("solid", fgColor=COLORS[r.Status])
            c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
            if j == 2:
                c.number_format = "#,##0.00"
    for col, w in zip("ABCDEFGHI", [3, 28, 12, 20, 4, 16, 4, 16, 4]):
        ws.column_dimensions[col].width = w


def _style_table(ws):
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    headers = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        status = row[0].value
        fill = PatternFill("solid", fgColor=COLORS.get(status, "FFFFFF"))
        for c in row:
            c.fill = fill
            c.border = Border(bottom=THIN)
            if headers[c.column - 1] in ("Gateway Net Amount", "Bank Credit", "Difference"):
                c.number_format = "#,##0.00;[Red]-#,##0.00"
    for i, h in enumerate(headers, start=1):
        width = {"Bank Narration": 42, "Remarks": 44, "Status": 24}.get(h, 17)
        ws.column_dimensions[get_column_letter(i)].width = width


# ---------------------------------------------------------------- email
def email_html(summary, biz_date, total_gw):
    matched = summary[summary.Status.str.startswith("Matched")].Count.sum()
    rows = "".join(
        f"<tr style='background:#{COLORS[r.Status]}'><td style='padding:6px 12px'>{r.Status}</td>"
        f"<td style='padding:6px 12px;text-align:right'>{r.Count:,}</td>"
        f"<td style='padding:6px 12px;text-align:right'>&#8377; {r.Amount:,.2f}</td></tr>"
        for r in summary.itertuples())
    return f"""<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#222">
<p>Hi team,</p>
<p>Gateway vs bank reconciliation for <b>{biz_date}</b> is complete.
<b>{matched:,} of {total_gw:,}</b> gateway transactions matched ({matched / total_gw:.1%}).
Exceptions needing review are below; full detail is in the attached Excel.</p>
<table style="border-collapse:collapse;border:1px solid #ccc">
<tr style="background:#1F3864;color:#fff"><th style="padding:6px 12px;text-align:left">Status</th>
<th style="padding:6px 12px">Count</th><th style="padding:6px 12px">Amount</th></tr>{rows}</table>
<p style="color:#666;font-size:12px">Automated report &middot; generated by reconcile.py</p></div>"""


def send_email(html, attachment: Path, biz_date):
    host, user, pwd, to = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not all([host, user, pwd, to]):
        log.info("SMTP not configured - email saved as HTML only")
        return
    msg = EmailMessage()
    msg["Subject"] = f"Gateway vs Bank Reconciliation - {biz_date}"
    msg["From"], msg["To"] = user, to
    msg.set_content("HTML email - please view in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(attachment.read_bytes(), maintype="application",
                       subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       filename=attachment.name)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)
    log.info("Email sent to %s", to)


# ---------------------------------------------------------------- pipeline
def run(gateway_path, bank_path, out_dir="output", send=True):
    """Run the full pipeline. Returns (results_df, summary_df, xlsx_path, email_html)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    gw, bank = load(Path(gateway_path), Path(bank_path))
    biz_date = gw.settlement_date.min().date().isoformat()
    log.info("Loaded %s gateway rows, %s bank rows", f"{len(gw):,}", f"{len(bank):,}")
    res = reconcile(gw, bank)
    summary = summarise(res)
    stamp = biz_date.replace("-", "")
    xlsx = out / f"Reconciliation_{stamp}.xlsx"
    write_excel(res, summary, gw, bank, xlsx, biz_date)
    html = email_html(summary, biz_date, len(gw))
    (out / f"summary_email_{stamp}.html").write_text(html, encoding="utf-8")
    res.to_csv(out / f"results_{stamp}.csv", index=False)
    log.info("\n%s", summary.to_string(index=False))
    log.info("Report written: %s", xlsx)
    if send:
        send_email(html, xlsx, biz_date)
    return res, summary, xlsx, html


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Gateway vs Bank reconciliation")
    ap.add_argument("--gateway", required=True)
    ap.add_argument("--bank", required=True)
    ap.add_argument("--out", default="output")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(args.gateway, args.bank, args.out)
    except Exception:
        log.exception("Reconciliation failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
