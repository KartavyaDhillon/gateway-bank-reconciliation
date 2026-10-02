# Reconciliation Runbook (SOP)

## Amount Mismatch
An amount mismatch means the bank credited a different amount than the gateway's net settlement for the same UTR.
Short credit: check whether the gateway deducted an extra fee, chargeback or refund adjustment. Raise a ticket with the gateway support team quoting the UTR and transaction ID within 2 working days.
Excess credit: usually a reversal of an earlier deduction or a batch error. Do not refund the customer. Confirm with the gateway before booking it as income.
Escalate any single mismatch above INR 10,000 to the finance controller the same day.

## Missing in Bank
The gateway marked the transaction as settled but no matching credit reached the bank.
First wait for the T+1 window: credits can land the next working day. If still missing after T+1, download the gateway settlement report and send the UTR list to the gateway's settlement team.
Missing amounts above INR 1,00,000 are high priority and must be escalated to the finance controller immediately.

## Unknown Credit
A bank credit with no gateway record. Possible causes: a settlement from a different gateway account, a manual transfer from a customer, or a refund reversal.
Search the UTR in all gateway dashboards. If not found, park the amount in the suspense account and do not allocate it to any client until identified.

## Duplicate Credit
The same UTR and amount was credited more than once. The bank or gateway will usually reverse it within 3 to 5 working days.
Do not use the duplicate amount for client payouts. Inform the bank relationship manager and track the reversal.

## Matched (T+1 credit)
Credited one day after settlement. This is normal for NEFT batches and weekend settlements. No action needed unless the delay becomes frequent for one gateway.

## Matched (amount + date)
The bank narration did not include a UTR, so the transaction was matched on a unique amount within the date window.
These are lower-confidence matches. Review them weekly and ask the bank to include UTR in narrations.

## Daily checklist
1. Confirm both files arrived and row counts look normal.
2. Review all exceptions, starting with Missing in Bank and large Amount Mismatches.
3. Update the exceptions tracker with owner and expected resolution date.
4. Re-run the reconciliation after corrected files arrive.
