# CreditPool

Issuer-controlled reallocation of unused hackathon program credits between teams, without a public credit exchange.

## Language

**CreditPool**:
The issuer-side product that configures a program, returns and reallocates credits, optionally records peer Trades with off-ledger Settlement, and reconciles against the issuer ledger.
_Avoid_: Exchange (when meaning a public credit venue), credit broker

**Issuer**:
The organization that owns the program budget and the rules for moving credits inside it (simulated as Nicrron for the demo).
_Avoid_: Provider, lab, sponsor (use only when speaking about the real-world counterpart)

**Program**:
One named credit grant with a total budget, per-team cap, eligibility list, and hard expiry.
_Avoid_: Campaign, event (the hackathon event is not the Program)

**Team**:
An eligible recipient of program credit inside one Program.
_Avoid_: User, account, participant (a participant acts for a Team)

**Credit**:
Program purchasing power denominated in USD on the issuer ledger; not a transferable third-party API key or promo code outside the Issuer.
_Avoid_: Token, key, OpenAI credit

**Return**:
An operation in which a Team releases unused Credit into the Program pool.
_Avoid_: Refund, withdrawal, sale

**Reallocate**:
An operation that moves Credit from a source (a Team or the Program pool) to a recipient Team inside the same Program.
_Avoid_: Sale, resale, transfer (when implying payment between users without a Trade)

**Trade**:
A peer agreement between two Teams to move Program Credit (via Reallocate) in exchange for money; the Credit leg still obeys Program Rules and Issuer approval.
_Avoid_: Purchase of credits, listing, order (unless speaking to UI controls)

**Settlement**:
The money side of a Trade: who pays whom and how much; for the hackathon this is recorded or demo-simulated and not custodied by CreditPool.
_Avoid_: Payout, withdrawal, payment rail (implementation)

**Offer**:
A Team's posted proposal to enter a Trade at a stated Credit amount and Settlement ask, waiting for acceptance.
_Avoid_: Listing, ask (alone), order

**Ledger Fingerprint**:
A checksum over Program and Team balances (and related Operation identity) used to detect Issuer Ledger drift after applies.
_Avoid_: Canary (UI label ok; glossary term is Ledger Fingerprint)

**Policy Preset**:
A named template of Program Rules values applied when configuring a Program.
_Avoid_: Policy engine, reset

**Issuer Ledger**:
The authoritative record of Credit movements for a Program; for the hackathon demo this ledger is simulated and does not move real Nicrron balances.
_Avoid_: Our database (the app's tables record requests; the Issuer Ledger is the money truth)

**Program Pool**:
A special Team inside a Program that holds Credit released by Returns and supplies Credit for Requests.
_Avoid_: Escrow, treasury (unless speaking to judges in plain language)

**Request**:
A Team-initiated Reallocate that draws Credit from the Program Pool to that Team, subject to rules and Reservation.
_Avoid_: Purchase, claim (when implying legal entitlement outside program rules)

**Operation**:
One Return or Reallocate request progressing through reserved, applied, and reconciled (or failed / flagged).
_Avoid_: Transaction, job, ticket

**Reservation**:
A time-bounded hold on a Team's Credit for one Operation that reduces Available Balance until applied, released, or expired.
_Avoid_: Lock, soft debit (implementation metaphors)

**Available Balance**:
Issuer Ledger balance for a Team minus its active Reservations; never allowed below zero.
_Avoid_: Spendable, free balance

**Grant**:
The Issuer's initial (or top-up) Credit entry to a Team under a Program; it remains on that Team until Returned, used, or expired.
_Avoid_: Deposit, funding (when meaning payment into our app)

**Admin**:
A person acting for the Issuer who configures the Program and may Reallocate Team → Team directly.
_Avoid_: Operator, maintainer

**Flagged Operation**:
An Operation paused for Admin review because a rule check failed or reconciliation mismatched; it must not leave half-applied purchasing power.
_Avoid_: Error (too vague), rejected (a Flagged Operation may still be resolvable)

**Reconcile**:
The step that verifies the Issuer Ledger, Operations, and Reservations agree after an apply; disagreement is a money failure.
_Avoid_: Sync, audit (Audit is the event trail; Reconcile is the balance check)

**Program Rules**:
The hard gates on every apply: Program budget, per-Team cap, eligibility, expiry, Available Balance, and same-Program only.
_Avoid_: Policy (too vague), terms of service

**Idempotency Key**:
A client-supplied key that identifies one logical Operation so retries return that Operation instead of creating another ledger move.
_Avoid_: Request id (unless it is this key)

**Audit Event**:
An append-only record of one step in an Operation's life (agent, Admin, or system), never updated or deleted.
_Avoid_: Log line, Reconcile (Reconcile checks balances; Audit Event records history)

**Nicrron API**:
The hackathon-required model endpoint used for agent reasoning; it is not the Issuer Ledger and does not move Program Credit in the demo.
_Avoid_: Nicrron (alone, when you mean Issuer or Credit)

**Agent Version**:
A labeled agent behavior (v1 unsafe vs v2 safe) used to demonstrate failure and fix; not a separate product.
_Avoid_: SKU, edition
