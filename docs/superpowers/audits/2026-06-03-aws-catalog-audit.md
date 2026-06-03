# aibuilder AWS catalog audit — 2026-06-03

**Status:** Draft
**Owner:** ChristopherN.Corbin@govtech.bb
**Trigger:** Real testing this week kept surfacing the same shape of bug — the catalog at `aibuilder/patterns.py` and `aibuilder/pricing.py` was leaning on stale training-data knowledge of AWS rather than current docs. App Runner deprecation, Amplify Gen 2 framing, missing bundled data layer — every fix made the catalog incrementally better but the next bug was always one repo away. This audit grounds the whole catalog against current AWS guidance so we can fix systemically, not reactively.

## Methodology

Three parallel AWS subject-matter-expert subagent runs, one per domain:
- **Container SME** — compute / container patterns (Fargate, Lambda, App Runner, ECS Express, EventBridge)
- **Serverless SME** — managed / serverless patterns (S3+CloudFront, Lambda+APIGW, Amplify Gen 2, EventBridge Scheduler)
- **Networking SME** — cross-cutting gaps (VPC, ALB, Route53+ACM, WAF, Secrets, observability)

### Verification status (important caveat)

The subagent contexts did NOT have access to the AWS MCP servers — `mcp__plugin_aws-dev-toolkit_awsknowledge__*`, `mcp__plugin_deploy-on-aws_awspricing__*`, `mcp__plugin_aws-serverless_aws-serverless-mcp__*`, `mcp__plugin_deploy-on-aws_awsiac__*` — even though those servers ARE loaded for the main session. Every subagent flagged this explicitly and grounded its findings in training-data knowledge with explicit "UNVERIFIED" markers on specific pricing / EOL / feature-surface claims.

**Action needed before publishing or shipping catalog changes:** spot-check the high-impact unverified claims (specifically the API Gateway, Lambda, and Amplify pricing numbers) from the main session, where the pricing MCP is available.

---

## Compute / Containers (Container SME findings)

### Service currency
- **App Runner deprecation**: As of mid-2026, AWS has communicated App Runner is being wound down with ECS Fargate (and ECS Express Mode for simpler config) as the migration path. Catalog's swap to Fargate / Lambda+APIGW is directionally correct. **Need to confirm exact EOL date** via the AWS What's New / End of Support feed before we cite one to users — current catalog phrasing "is being deprecated by AWS" is informal.
- **ECS Express Mode** as the App Runner replacement is correct — AWS-blessed "simpler config" Fargate experience.
- **Mangum for FastAPI/Flask on Lambda**: still community-maintained, not deprecated, but AWS now points to **Lambda Web Adapter (LWA)** as the recommended path (same container runs locally / Lambda / Fargate, zero code changes). Recommend updating `python_api` notes.
- **EventBridge Scheduler** (vs legacy EventBridge Rules) is current and correct for `worker`.
- **API Gateway**: catalog says generic "API Gateway" — should specify "HTTP API" everywhere it appears.

### Sizing realism
- **Fargate 0.25 vCPU / 0.5 GB**: still valid x86 Linux minimum. Fine for prototype.
- **Lambda 256 MB / 200 ms**: realistic billed-duration estimate for warm Node handlers; optimistic for cold-start FastAPI under Mangum (500ms–1.5s cold, 50–150ms warm). Acceptable for cost math; don't use for SLO claims.
- **Worker Lambda 512 MB / 5000 ms / 720 invocations**: sane for hourly cron.

### Pricing reality check (UNVERIFIED — needs main-session re-validation)

| Pattern | Service | Catalog $/mo | Best-effort actual $/mo | Within 25%? |
|---|---|---|---|---|
| `*_api` | API Gateway HTTP API @ 100k req | $0.35 | ~$0.10 ($1/M × 0.1) | **NO — 3.5x high.** Likely mislabeled REST API price |
| `*_api`, `worker` | Lambda @ 100k inv, 256 MB, 200 ms | $0.10 | ~$0.00 in free tier; ~$0.02 after | NO — 5x high but rounds to "negligible" |
| `dockerized_web`, `fullstack_with_db` | Fargate 0.25 vCPU / 0.5 GB, 730 hr | $9.00 | ~$9.13 (calc'd) | **YES** — within 2%. Good. |
| `worker` | EventBridge Scheduler 720 inv | $0.00 | $0.00 (14M/mo free) | YES |

### Architectural completeness gaps
For each Fargate-based pattern (`dockerized_web`, `fullstack_with_db`), the minimum-viable production deploy ALSO needs (and the catalog should mention in `notes`):
- VPC with public + private subnets across AZs (networking SME covers)
- ALB + target group + listener (HTTP→HTTPS redirect, ACM cert)
- ECS cluster, service, task definition
- Task execution role (ECR pull, CloudWatch Logs write) AND task role (app permissions)
- Security groups: ALB-SG (0.0.0.0/0:443) and task-SG (ALB-SG:port only)
- CloudWatch log group with retention (default is never-expire — cost trap)
- ECR repo + lifecycle policy

For Lambda+APIGW patterns: execution role, CloudWatch log group **with retention**, reserved concurrency consideration, custom domain / ACM cert if not using the default `execute-api` URL.

### Recommended alternatives to surface
Add to existing `notes`:
- `dockerized_web` + `fullstack_with_db`: "**Fargate Spot** can reduce compute up to ~70% for stateless workloads that tolerate 2-min interruption."
- `node_api`: "For real-time / bidirectional traffic, **API Gateway WebSocket API** + Lambda; for SSE / long-lived HTTP, prefer ALB+Fargate."
- `python_api`: "**Lambda Web Adapter** lets you ship the same FastAPI/Flask container to Lambda without Mangum."
- `worker`: "For fan-out or queue-driven workers, **SQS → Lambda** with event-source mapping is usually a better fit than Scheduler."

### Net-new patterns to consider
- **`tiny_container`** (or alt note on `dockerized_web`): Lambda + Lambda Web Adapter + Function URL. Scales to zero, no ALB cost (~$16/mo saved), 10 GB image limit, 15-min request cap. Strong prototype fit.
- **`worker_long`** as first-class pattern: Fargate scheduled task for ETL/report jobs that exceed Lambda's 15-min cap.

---

## Serverless / Managed Hosting (Serverless SME findings)

### Amplify Gen 2 reality check (UNVERIFIED specifics flagged)
- Product name `"AWS Amplify (Gen 2)"` in the catalog is fine; AWS uses "AWS Amplify Gen 2" or "Amplify (Gen 2)" interchangeably.
- **Gen 1 status**: read is "feature-complete, no new investment" rather than a formal EOL announcement. Current catalog wording "is in maintenance mode" is directionally correct but slightly stronger than what AWS officially says. Recommend softening to *"Gen 1 (Studio + CLI) is no longer the recommended starting point; AWS guides new projects to Gen 2."*
- **Primitives**: `defineData` / `defineAuth` / `defineStorage` / `defineFunction` all correct. `defineBackend` is the root composition function and is missing from the catalog notes — worth adding.
- **Data backend options**: catalog says "AppSync + DynamoDB"; this is incomplete. AWS subsequently added a Postgres/MySQL connector — `defineData` can now back AppSync resolvers against an existing RDS/Aurora cluster. Recommend updating notes to *"AppSync resolvers backed by DynamoDB by default, or by an existing RDS/Aurora SQL database via the SQL data source."* (UNVERIFIED — re-check SQL data source GA status.)
- **Framework support**: Next.js 14+, Nuxt 3, Remix, Astro, SvelteKit, plain Vite. SSR via Lambda@Edge-style compute behind CloudFront. UNVERIFIED for exact max bundle size / SSR memory ceiling.

### API Gateway: HTTP vs REST
- **HTTP API is the correct default** for `spa_with_api`, `node_api`, `python_api`. ~70% cheaper, lower latency, simpler config.
- **Pick REST API only when** the user needs: API keys + usage plans, request/response transformation, native AWS WAF on the API stage, edge-optimized endpoints, private (VPC) APIs with resource policies, full JSON Schema validation, or X-Ray on the stage itself. None fit the prototype use case.
- Recommend renaming the service label in catalog from `"API Gateway"` → `"API Gateway (HTTP API)"`.

### Mangum vs Lambda Web Adapter for Python on Lambda
- **LWA is now the AWS-blessed path** for running existing ASGI/WSGI apps on Lambda unchanged (AWS Labs project, promoted in AWS Compute Blog and Lambda dev docs). Works for FastAPI, Flask, Django, Express, Next, Nuxt — zero code changes, ships as a layer or container extension.
- Mangum still works but no longer the default AWS recommends.
- For `python_api`: *"FastAPI/Flask via Lambda Web Adapter (zero-code-change). Mangum is an alternative if you prefer a pure-Python adapter and don't want a layer."*
- Selling point: LWA also runs identically inside Fargate/Express Mode containers, so the same code can move container-ward later.

### S3 + CloudFront for static / SPA
- **OAC is the current recommendation** (OAI in maintenance). The deploy-agent's Terraform stack already uses OAC. Catalog notes for `static_site` / `spa_with_api` don't mention this — add a one-liner: *"CloudFront → S3 is locked down via Origin Access Control (OAC); bucket stays private."*
- **SPA client-side routing**: every SPA needs CloudFront `custom_error_response` mapping 403 + 404 → `/index.html`. The infra stack has `is_spa` toggle for it. Add to `spa_with_api`: *"SPA mode rewrites 403/404 to index.html so client-side routes work on refresh."*
- **CloudFront response headers policy** for basic security headers (CSP, HSTS, X-Content-Type-Options) — free, one resource, big audit win for a GovTech tool.

### EventBridge: Scheduler vs Rules
- **Scheduler is correct for `worker`** — AWS-preferred for new scheduled invocations (finer schedule expressions, per-schedule IAM, DLQ, time zones, 1M-schedules-per-account cap).
- Price difference at 720 inv/mo is rounding error.

### Pricing reality check (UNVERIFIED — needs main-session re-validation)

| Pattern | Service | Catalog $/mo | Calc from list price | Within 25%? |
|---|---|---|---|---|
| `static_site` | S3 (1 GB + 10k GET) | $0.10 | ~$0.03 | NO — over by ~3x, but rounding-tier fine |
| `static_site`, `spa_with_api` | CloudFront (5 GB out + 100k req) | $0.50 | $0.00 in free tier; ~$0.43 after | YES (post free-tier) |
| `spa_with_api`, `*_api` | API Gateway HTTP API (100k req) | $0.35 | ~$0.10 | **NO — 3.5x high.** Drop to $0.10. |
| `spa_with_api`, `*_api` | Lambda (100k inv, 256 MB, 200 ms) | $0.10 | $0.00 free tier; $0.02 after | NO — 5x high, but rounds to "negligible" |
| `nextjs_amplify_hosting` | Amplify Gen 2 (100k req + 5 GB + 1 GB-h SSR) | $3.00 | ~$0.75–$1.50 | NO — ~2-3x high. Tighten to $1.50. |
| `worker` | EventBridge Scheduler (720 inv) | $0.00 | $0.00 | YES |

### Architectural completeness gaps for Amplify Gen 2
- **GitHub connection** required up front via Amplify console (OAuth app install) — not something the deploy agent can do silently. Worth a note: *"You'll be asked to connect GitHub once per AWS account; subsequent deploys are push-to-deploy."*
- **Branch-based deploys + preview environments**: key Gen 2 selling point, not mentioned. Add a bullet.
- **Env vars / secrets**: set per branch in Amplify console or `amplify/backend.ts`. Common stumbling block.
- **CDK escape hatch**: `backend.addOutput` + direct CDK constructs let users mix their own CDK. Worth mentioning for sophisticated repos.
- **`amplify.yml` build settings**: auto-detected for Next/Nuxt/Remix; monorepos need explicit config.

For `spa_with_api`: catalog says CloudFront "proxies API traffic" but doesn't specify same-origin (CloudFront behavior path) vs cross-origin (CORS on APIGW). Same-origin via behavior is cleaner; make the choice explicit.

### Net-new options to add
1. **Lambda Function URLs** — simplest `*_api` case: single endpoint, no auth or IAM, no usage plan. Cheaper than HTTP API, faster cold path. Good fit for webhook receivers, simple JSON APIs. Should be a 4th `*_api` variant.
2. **CloudFront Functions** — sub-ms edge compute for URL rewrites, header manipulation, A/B routing. $0.10/M invocations. Worth adding to `static_site` / `spa_with_api` as optional layer for redirects (www→apex, /old→/new).
3. **Step Functions Express** — workflow-shaped workers (fan-out, retry, parallel branches). $0 base, per-execution + duration. `worker` is single-Lambda only today — add `workflow_worker` when the repo signals orchestration.
4. **Lambda@Edge** — **don't** add. Niche, slow deploys, hard to debug; CloudFront Functions covers 90% of use cases at less cost.
5. **SQS-triggered Lambda** — missing async-job pattern. Current `worker` is schedule-driven; queue-driven is a different shape (web pushes job, worker drains).

---

## Networking / Edge / Security Baseline (Networking SME findings)

### What every internet-facing pattern is missing
The catalog treats compute + data as the whole story and leaves the internet-facing edge invisible. Across every pattern except the two CloudFront ones, users have no idea how traffic reaches their app, where TLS terminates, or what protects it. Cross-cutting adds needed:
- TLS termination strategy named explicitly (today only "HTTPS" appears as a wave)
- A custom-domain story (Route53 hosted zone + ACM cert + alias record)
- An implied IAM note so users aren't surprised by execution/task role / bucket policy footprint
- "Logs are on by default" acknowledged so users don't think CloudWatch costs are a bug

### Pattern-specific networking gaps

**`dockerized_web` (Fargate-only):** Biggest gap. Fargate requires a VPC, ENIs in subnets, security groups — none mentioned. With no ALB and no CloudFront, the only public-exposure path is `assignPublicIp=ENABLED` in a public subnet → ephemeral public IP that changes every deploy, no HTTPS. That's a footgun. Either add ALB + ACM as part of the pattern (~$16/mo ALB minimum + LCU), or recommend CloudFront in front via VPC Origins. Also need a **minimum VPC blurb**: "1 VPC, 2 public subnets across 2 AZs, 1 SG; no NAT Gateway needed if tasks live in public subnets and pull from public ECR via the IGW." NAT Gateway is the silent $32/mo budget killer.

**`fullstack_with_db`:** Three real omissions:
1. **ALB is named in the `purpose` string but absent from `services` and `_FALLBACK_PRICES`.** Estimate is off by ~$16–20/mo.
2. **RDS must be in a private subnet** with a DB security group accepting traffic only from the Fargate task SG. Catalog says nothing — users may put RDS in a public subnet with `0.0.0.0/0` for "easier dev access."
3. **DB credential delivery** undefined — no Secrets Manager / Parameter Store entry, so users will hardcode passwords in task env vars.

**`node_api` / `python_api`:** Functionally complete for HTTPS (APIGW gives free `*.execute-api` URL with TLS), but catalog never tells users how to put their own domain on it. Recipe: APIGW custom domain + ACM cert (same region) + Route53 alias. No VPC needed (good, keep it that way).

**`static_site` / `spa_with_api`:** Healthiest patterns. Main gap: **CloudFront → S3 access control** — catalog doesn't pin "use OAC." Also `spa_with_api` doesn't say HOW CloudFront proxies to APIGW (origin → APIGW custom domain, cache behavior on `/api/*`) — DIY users will hit CORS hell.

**`nextjs_amplify_hosting`:** Amplify hides almost all of this (TLS, edge, custom domain via console, WAF integration is a paid add-on). One note: *"custom domain is managed inside Amplify; no separate Route53 work needed if the zone already exists in the account."*

### CloudFront-in-front recommendation
- **`*_api` patterns:** CloudFront in front of APIGW is **optional, not required**. APIGW already gives TLS, regional anycast-ish performance, basic throttling. Add CloudFront when user needs (a) edge caching of GETs, (b) one hostname covering frontend + API, or (c) WAF at edge. For prototypes, overkill — add as a `notes` line, not a service.
- **`dockerized_web`:** CloudFront in front of an ALB is the cleanest "HTTPS + domain + DDoS basics" answer; lets users skip the ACM-on-ALB dance (cert lives on CloudFront in us-east-1). **Recommend adding CloudFront by default** rather than leaving Fargate naked.

### Custom domain story (cross-pattern)
Generic enough to be a shared note appended to every internet-facing pattern:

> Bring your own domain by creating a Route53 public hosted zone ($0.50/mo) and an ACM certificate (free). For CloudFront and APIGW Edge endpoints, request the cert in us-east-1. For ALB and APIGW Regional endpoints, request it in the same region as the resource. Point the domain via a Route53 alias record.

Implement as a single `CUSTOM_DOMAIN_NOTE` constant injected into every pattern except `worker`.

### Secrets management
For prototypes, **default to SSM Parameter Store (Standard tier, free)** for DATABASE_URL, API keys, etc. Integrates natively with ECS task definitions (`secrets:`) and Lambda (`valueFrom`). Recommend **Secrets Manager (~$0.40/secret/mo)** only when user needs automatic rotation (RDS), cross-account sharing, or KMS-per-secret separation. Add a note on `fullstack_with_db` specifically — only catalog pattern with a real credential today.

### Observability baseline
CloudWatch Logs + Metrics is on-by-default for Lambda, ECS, APIGW, ALB, RDS. Prototype traffic stays free (5 GB logs ingest / 10 metrics / 10 alarms). Don't add a cost line; add a one-sentence note per pattern: *"CloudWatch Logs and basic metrics are enabled by default; prototype traffic stays inside the free tier."*

X-Ray: **not worth surfacing for prototypes** ($5/1M traces + instrumentation friction). Mention only on `spa_with_api` and `fullstack_with_db` as optional add-on for cross-service latency debugging.

### WAF / security upgrades
Skip WAF for prototype tier ($5/mo WebACL + $1/rule + $0.60/1M req dominates the budget). For "internal GovTech tool" persona it becomes table stakes — add a `notes` line on internet-facing patterns: *"For production / GovTech-internal traffic, attach AWS WAF to CloudFront or the ALB (~$5–10/mo baseline) for managed OWASP rules and rate-based bot protection."* Don't price it in by default.

### Net-new patterns to consider
- **`CUSTOM_DOMAIN_NOTE` shared constant** wired into every applicable pattern.
- **`VPC_BASELINE_NOTE` shared constant** for `dockerized_web` + `fullstack_with_db`.
- **`internal_tool` variant** of `fullstack_with_db`: swap public ALB for internal ALB + Client VPN or Cognito ALB auth. Likely the right shape for GovTech-internal apps; would justify WAF + Secrets Manager + private subnet defaults.

---

## Cross-cutting synthesis

Themes that recurred across all three SME reports:

1. **Pricing is systematically over-estimated.** API Gateway 3.5x high, Lambda 5x high, Amplify 2–3x high. Only Fargate, EventBridge, RDS are accurate. Real bug, cheap to fix (one file, ~5 lines).

2. **The catalog hand-waves the "supporting infrastructure" every pattern actually needs** — VPC, ALB, IAM roles, security groups, CloudWatch log retention, ECR repo. Real production gaps users will hit. Either include the supporting services as explicit catalog lines (and own the higher cost) or add a "what you also pay for" note per pattern.

3. **Custom domain story is missing across all patterns.** Universal recipe (Route53 hosted zone + ACM cert + alias). Should be a shared note constant.

4. **Secrets management is missing.** SSM Parameter Store is the right default; Secrets Manager only when rotation/cross-account/KMS-per-secret needed.

5. **HTTP API vs REST API ambiguity.** Catalog says "API Gateway" generically; the LLM could pick wrong. Specify "API Gateway (HTTP API)" everywhere.

6. **Lambda Web Adapter > Mangum for python_api.** Current AWS recommendation; same container moves to Fargate later.

7. **OAC > OAI for S3+CloudFront.** Add to `static_site` / `spa_with_api` notes.

8. **SPA error handling missing.** CloudFront custom_error_response 403/404 → index.html. Add to `spa_with_api` notes.

9. **App Runner EOL date unknown.** Soften phrasing until we have the actual date.

10. **Useful new patterns to add:** Lambda Function URLs, Lambda Web Adapter on Function URL (tiny_container), Step Functions Express (workflow_worker), SQS-triggered Lambda (queue_worker), `internal_tool` variant for GovTech use cases.

---

## Prioritized punch list

### Quick wins (small file edit, high impact) — 1 commit each
1. **Fix the three over-estimated prices** in `_FALLBACK_PRICES`:
   - API Gateway $0.35 → $0.10 (was effectively REST-API price labeled as HTTP)
   - Lambda $0.10 → $0.02 (or call it "$0.00 in free tier")
   - Amplify Gen 2 $3.00 → $1.50
2. **Rename `"API Gateway"` → `"API Gateway (HTTP API)"`** in all catalog entries; update pricing key.
3. **Add `OAC` note** to `static_site` and `spa_with_api`.
4. **Add SPA error-handling note** to `spa_with_api`.
5. **Soften App Runner EOL phrasing** in `dockerized_web` + `fullstack_with_db` notes (drop the parenthetical) until we have a sourced date.
6. **Add Lambda Web Adapter mention** to `python_api` notes; promote LWA as the recommended path, demote Mangum to "alternative."

### Medium fixes — 1 commit each
7. **`CUSTOM_DOMAIN_NOTE` shared constant** appended to every internet-facing pattern.
8. **Secrets management note** on `fullstack_with_db` (SSM Parameter Store default, Secrets Manager for rotation).
9. **CloudWatch retention note** on Lambda + Fargate patterns ("default is never-expire — set 7-day retention").
10. **VPC baseline note** for `dockerized_web` + `fullstack_with_db` (1 VPC, 2 public subnets, no NAT for prototypes).
11. **Amplify Gen 2 completeness pass** — add GitHub-connection note, branch previews, env vars, `defineBackend`, SQL data source.

### Bigger work — multi-task plans
12. **Decide ALB-in-`fullstack_with_db` story:** include ALB as an explicit service (cost goes ~$21 → ~$37/mo, honest), OR move to "CloudFront in front of Fargate via VPC Origins." Needs a product call.
13. **Add CloudFront-in-front for `dockerized_web` by default** (default-on for the HTTPS + domain + DDoS story).
14. **Add `tiny_container` pattern** (Lambda + LWA + Function URL) as a `dockerized_web` alternative for "I have a small Dockerfile."
15. **Add `workflow_worker` pattern** (Step Functions Express) for orchestration-shaped workers.
16. **Add `queue_worker` pattern** (SQS-triggered Lambda) for event-driven workers.
17. **Add `internal_tool` variant** of `fullstack_with_db` (internal ALB + Cognito ALB auth + WAF + private subnets).
18. **Live AWS Pricing API integration** (Phase 1.5 from the original spec) — replaces all fallback prices with real-time quotes; makes "is_fallback" actually meaningful.

### Cross-cutting (one-time research)
19. **Spot-check the three over-estimated prices** against the live AWS Pricing API from the main session (the MCP IS available here, just wasn't in subagent contexts). Confirm the SME numbers before publishing #1.
20. **Confirm Amplify Gen 2 SQL data source GA status** — affects whether the AppSync-on-RDS note in #11 ships.
21. **Confirm App Runner formal EOL date** — would let #5 cite a date instead of softening.

---

## Open questions for human review

- **MCP coordination:** subagent contexts didn't inherit the AWS MCP tools that ARE loaded in the main session. Worth investigating whether this is configurable, or whether AWS-grounded research should always happen in the main session and the SMEs should be used only for synthesis. (Possible workaround: pass relevant MCP outputs as context strings into the subagent prompts.)
- **Verified-vs-fallback pricing as a UX surface:** should the catalog/cost output distinguish "rounded-up safety estimate" from "list-price accurate"? Current numbers conflate the two; users may be surprised either way.
- **GovTech-internal personas:** does the user want a separate `internal_tool` family of patterns? Different defaults (private subnets, WAF, Cognito auth, Secrets Manager) would justify it.
- **Plan H (rate limits) interaction:** if we add 3+ new patterns, rate-limiting per-pattern vs global needs a call.
