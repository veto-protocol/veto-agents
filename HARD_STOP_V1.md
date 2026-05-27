# HARD_STOP v1 — the real enforcement loop

**Status:** spec, not built.
**Owner:** Tomer (Investech Global).
**Target:** Base Sepolia end-to-end demo in 2 weeks; mainnet alpha (bug-bounty, no paid audit) in 3 weeks.

---

## Why this exists

Veto today produces *evidence* — signed Ed25519 receipts that an authorize call happened and what the verdict was. That's useful but it isn't the product. The product is **enforcement**: the agent literally cannot move the user's money beyond what the user allowed. A receipt that says "denied" while the agent still moves the funds is no protection at all.

The one `VetoGuardedAccount` deployed on Base Sepolia at `0xCBbbC4b924AF40D29f135c3a88b6F650d55d92c5` proves the primitive works. It is not a per-user product. The work below turns that proof into something every veto-agents user gets automatically.

We are building this loop *inside veto-agents* because:

1. We need a real spend to gate, and veto-agents naturally produces them.
2. Three demos for one build (Veto loop, agents loop, hard-stop loop).
3. Forces every design decision to be honest about the user experience.

---

## The loop, end to end

A non-technical user, fresh machine:

1. `curl …/install.sh | bash` → veto-agents installed.
2. `veto-agents` → picks an agent, signs in via magic link.
3. `veto-agents wallet setup` → browser opens to a hosted Privy page. User signs in with the same email; an embedded wallet is created; a **Safe** is deployed with the **Veto Guard** module installed in the same transaction. Gas paid by a sponsor (paymaster). User sees: *"Wallet ready. Address: 0x… on Base Sepolia."*
4. User funds the Safe with testnet USDC (faucet) or, on mainnet, real USDC via fiat onramp.
5. `veto-agents media "<brief>"` → agent runs. For any spend that involves USDC moving from the Safe (an x402 API call, a tip to a fixed address, a future Stripe-Issuing-backed card auth):
   - Agent asks Veto for a signed mandate.
   - Veto's engine evaluates against the user's policy.
   - On **allow**: Veto signs the mandate. Agent submits a Safe transaction including Veto's signature. The Guard verifies the signature on-chain. The Safe executes.
   - On **deny / escalate**: Veto refuses to sign. Agent has no signature → if it tries to submit anyway, the Guard reverts on-chain. Funds do not move.

The on-chain refusal is the part that matters. Today a sophisticated agent could theoretically ignore Veto's verdict and move funds itself. After v1, it can't.

---

## What we are NOT shipping in v1

These are real things Veto should have eventually. They are out of scope for this 2-week spear so we don't drift:

- **Stripe Issuing path for card-based spends** (Meta ads, Google ads, web checkouts). Different rail, different mechanism. Saved for v2.
- **Solana hard-stop.** The Solana program exists but Privy + Safe + Guard is EVM-native. Pick one chain, prove it works, port second. v1 = Base only.
- **Mainnet with full third-party audit.** v1 = testnet end-to-end + a minimal-Guard mainnet alpha with a bug bounty. Real audit comes later if usage justifies the spend.
- **Multi-sig / quorum policies.** v1 = single owner + Veto co-signer. Multi-owner policies come later.
- **Hardware wallet path.** v1 = Privy embedded only. Existing-wallet path stays at "paste address, Safe will deploy later."

---

## Five-phase build plan (~14 days, full-time)

### Phase 1 — Hosted wallet page (3 days)
**Where:** `frontend/src/pages/WalletSetup.tsx` (Veto's main frontend).

The placeholder `/wallet/setup` currently 404s. Replace it with:
- Privy email-login flow (same auth as the magic-link CLI sign-in — single account across CLI + web).
- After login: triggers Safe + Veto Guard deployment via the Privy embedded wallet + Safe SDK. Single transaction. Paymaster pays gas.
- Posts the resulting Safe address back to a Veto endpoint keyed by the CLI's device-code.
- CLI polls the same endpoint, gets the address, saves it to keychain + config.

**Deliverable:** `veto-agents wallet setup` opens the browser, the user clicks once, the CLI ends up with a deployed Safe address.

### Phase 2 — The Guard contract (4 days)
**Where:** `contracts/src/VetoGuard.sol` (new file in `veto-protocol/contracts`).

A **minimal** Safe-compliant Guard module. Target: under 100 lines of Solidity.

```solidity
interface IGuard {
    function checkTransaction(...) external;
    function checkAfterExecution(bytes32 txHash, bool success) external;
}
```

What the Guard verifies in `checkTransaction`:
1. The transaction's `data` includes a Veto mandate signature (passed via Safe's `signatures` field or a wrapper contract).
2. The signature is over `(chain_id, safe_address, nonce, calldata_hash)`.
3. The signer matches Veto's published co-signer address (read from the Guard's own storage; can be rotated by a user-controlled timelock).
4. Replay protection: nonce strictly increases.
5. Escape hatch: if Veto's co-signer hasn't moved in N days, the user can unilaterally remove the Guard via Safe's `setGuard(0)`.

What the Guard does NOT do: hold funds, execute logic, touch external contracts. Single responsibility = signature check + revert. This keeps the attack surface small enough to ship with a bug bounty instead of a paid audit.

Tests in Foundry: replay attempt, missing signature, wrong signer, escape hatch, integration with Safe's `execTransaction`. Targeting 100% branch coverage on the Guard itself.

**Deliverable:** `VetoGuard.sol` deployed to Base Sepolia, address published in the repo. Test suite green.

### Phase 3 — Backend mandate signing (2 days)
**Where:** `gateway/views.py` + a new signer module.

When `/api/v1/authorize/` returns `allow` for an on-chain spend, the response includes a **secp256k1 signature** over the Safe transaction's `(chain_id, safe_address, nonce, calldata_hash)`. Today the response includes an Ed25519 JWT for the off-chain receipt — that stays — but on-chain spends additionally get a Safe-Guard-compatible signature.

The signing key is held in Veto's KMS (already exists for receipt signing; just adds a secp256k1 key alongside the Ed25519 one). The Guard's stored signer address points at the public key of this new secp256k1 key.

**Deliverable:** Authorize responses include a `safe_signature` field when the action is on-chain. CLI / agent code uses it when submitting the Safe transaction.

### Phase 4 — Agent + CLI plumbing (3 days)
**Where:** `veto-agents/src/veto_agents/agents/media/agent.py` + new `veto_agents/safe_tx.py`.

Add a single on-chain spend the media agent makes per run, for the demo:
- After image generation: agent sends `$0.10 USDC` from the user's Safe to a fixed "demo merchant" address (Veto's treasury for now). Real on-chain spend. Replaces or supplements the tipping concept I mentioned earlier.
- Agent calls `client.authorize(action="payment", ...)`, gets `safe_signature`, assembles the Safe transaction, submits via the user's Privy session key.
- On success: real on-chain tx, receipt URL points at Veto's receipt page, transaction hash links to Basescan.
- On deny: Veto refuses to sign, agent doesn't even attempt the Safe tx. If we force a submit (debug flag), the Guard reverts with `VetoSignatureMissing()`.

Same plumbing extends to x402 spends later (different destination, same Safe transaction shape).

**Deliverable:** A real on-chain tx from `veto-agents media` on Base Sepolia, visible on Basescan, governed by the Guard.

### Phase 5 — Demo + docs (2 days)
**Where:** README, a demo script, a screen recording.

The narrative recording:
1. Cold install on a fresh machine.
2. Walk through sign-in + wallet setup + funding.
3. Run media agent with a $5/day cap, watch the first 3 spends allow.
4. Bump the next spend over the cap. Show Veto's deny verdict.
5. Force-submit the Safe tx anyway via a debug flag. Show the on-chain revert. Link to Basescan.
6. "The protection is in the contract, not in the verdict. Even if the agent ignored Veto, the wallet itself refused."

**Deliverable:** A public Loom / mp4 of the full loop. Probably 90 seconds. This is what we share when someone asks "what does Veto actually do."

---

## Open decisions still owed

1. **Mainnet timeline.** Testnet v1 in ~2 weeks. Mainnet alpha (bug bounty, no audit) — Tomer's call: ship 1 week after testnet works, or wait longer for an audit?
2. **First demo merchant address.** Veto's treasury, or pick something more visceral (donate to a public charity, mint a public NFT, send to a fixed art-DAO address)? Doesn't matter technically, matters for the narrative.
3. **Existing-wallet path.** When v1 ships, the "I have my own Metamask/Rabby" path still has the limitation that we don't deploy a Safe for them automatically (they'd need to deploy + install the Guard via `app.safe.global`). Acceptable for v1 or block on it?
4. **Recovery story.** Privy's email-recovery is the default. Do we add a secondary recovery method (e.g., a recovery passphrase) before mainnet, or trust Privy's default?

These don't block Phase 1-3. They block the "go to mainnet" call.

---

## Where this leaves veto-agents

After v1 ships, every `veto-agents` user has a real Safe with real funds and real enforcement. The earlier work (sign-in, LLM picker, keychain, agent install) all stays — none of it is wasted. It's the *path that leads to* the hard-stop demo. The CLI polish from versions 0.0.15–0.0.19 was the foundation, this is what makes that foundation matter.

The pitch evolves:

- **Before v1:** "Veto signs verdicts on every agent action. Audit trail."
- **After v1:** "Your agents try to spend your money. Veto signs verdicts AND the chain enforces them. Your wallet physically refuses unauthorized spends."

Same primitive, different teeth.
