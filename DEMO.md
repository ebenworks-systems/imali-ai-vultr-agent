# Three minutes with the month-end agent

Open https://158-247-235-244.sslip.io in a browser. The page opens your own copy of the books of Lesedi Ridge Build Supply, a building-supply merchant, for two hours. Changes stay in your copy, and Start over gives you a fresh one at any time.

## The walk

1. The books (0:00): The figures at the top show the bank balance, what contractors owe, what the shop owes its suppliers, cash on hand and what waits for approval.
2. A typed sale (0:10): In Your agent, tap "Sold 20 bags of cement for R2,300 cash" under Try saying. Your agent drafts the sale with its debit and credit. Press Approve, and the cash-on-hand figure moves.
3. A voice note (0:30): Tap the microphone, allow it once, and say "Paid 850 rand cash for fuel for the delivery truck". Tap again to stop and send. Vultr's Whisper model turns it into words, your agent drafts the cost under Fuel and vehicle costs, and you approve it. The voice note is deleted as soon as it is turned into words.
4. A question (0:55): Ask "Who owes me money?". Your agent answers from the books, contractor by contractor, with what is late.
5. Close my month (1:05): Open Close my month and press Close my month. The five steps stream in as they happen, in about half a minute, and the README lists what each one does. The statement is read in a locked-down container with no internet access before any model sees it.
6. What needs you (1:30): Needs you comes first. Read the card for the second payment to Thabeng Roof Sheeting: it is the same amount and wording two days after the first, and approving it books the money as owed back to the shop. Read why each amount mismatch was flagged, then pick an account for the internet transfer and approve it, or press Reject to set it aside.
7. The rest in two presses (1:55): Under Matched, press Approve all 10 matched lines. Under Account suggested, check the account Vultr's models suggested for each cost and press Approve all 16 suggested accounts.
8. Follow-ups (2:10): Under Follow-ups, each late contractor has a message to copy into your own chat. Press Copy the message. Your agent sends nothing.
9. Post and summarise (2:20): Press Post approved entries. The summary shows the month's money in and out, who owes, and the checks: the bank balance in the books equals the statement's closing balance, and the two sides of the books are equal.
10. The books behind it (2:40): In Books, open the journal, the trial balance and the audit trail, which records every step you and your agent took.

## Entry text

The 120-word description for the event's entry form:

> Imali AI's month-end agent closes a shop's books on a Vultr VM in Seoul. The owner tells their agent about a sale or cost by text or voice note, and it drafts the record. Close my month runs five steps on the server: it reads the bank statement in a sandbox with no network, matches each line to invoices, bills and cash-ups, flags duplicate payments and mismatched amounts, lists who owes with follow-ups to copy, and posts approved entries to balanced double-entry books. Vultr Serverless Inference turns voice notes into text with Whisper, ranks names and accounts with Qwen embeddings and the BGE reranker, and screens every input with Nemotron. Code decides every match, and the owner approves each entry.
