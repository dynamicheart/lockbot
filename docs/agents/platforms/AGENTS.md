# Assist messaging-platform onboarding

## Shared workflow and authority

Keep the enterprise application, resource bot and resource group distinct. Use the selected Lockbot service's current UI and documentation. Do not invent HTTP endpoints or guess identities from display names. Feishu is included in official 3.x images; older Infoflow-only deployments must upgrade first. Verify the actual installed version before onboarding.

An Agent can explain fields, open approved consoles, prepare configuration, check an existing application's granted scopes, guide the user through approval, and test a specifically authorized group. Enterprise login/MFA, terms, administrative approval, secret entry and permission grants remain governed by the host's action/confirmation rules. Never approve the application as another person, bypass review or request a broader directory range to make a test pass.

The selected messaging platform is fixed when creating a resource bot. To migrate from Infoflow to Feishu, use configuration copying into a **new** bot. Do not copy keys, group/application associations, Agent authorizations, platform-specific user lists, lock state or history. Do not run two independent allocators over the same physical resources during transition.

## Infoflow / 如流

1. Have the authorized administrator create or select the group robot in Infoflow, enable receiving messages, and obtain its Webhook URL, Token and AES key through that platform's settings. Store them privately; do not substitute Feishu credentials.
2. Create the corresponding resource bot in Lockbot, choose `Infoflow`, configure nodes/devices and enter its platform credentials. Review renewal/cap/privacy settings before starting it.
3. Use the callback URL displayed by **that created bot** in Lockbot, normally `/api/bots/webhook/{bot_id}`, in Infoflow's receiving-message settings. Do not copy another bot's callback or assume an ID from an old service.
4. In the explicitly authorized test group, verify a real @ query and a short fictional resource lock/release. Then verify the resource webpage, notification and binding workflow separately. Callback reachability, signature validation and correct bot/resource routing are separate checks.

## Feishu / 飞书

Follow the [Feishu integration guide](../../usage/feishu.md) when present in the selected release. The current integration model uses one enterprise self-built application for multiple resource groups, each explicitly mapped to an independent resource bot. Super administrators manage the application and authorize group routes; ordinary bot administration does not grant access to other people's private Agent notes.

1. In Feishu Open Platform, select the correct enterprise and create/select a self-built app. Add bot capability. Obtain App ID and App Secret from **Credentials and Basic Information**; an arbitrary Lockbot application name is only a management label. Never display a plaintext secret in screenshots.
2. Configure the intended app availability and data scopes. Use the following application-identity scopes for the complete group-management experience:

   | Scope | Role |
   | --- | --- |
   | `im:message.group_at_msg:readonly` | Receive real group @ messages |
   | `im:message:send_as_bot` | Send replies and reminders |
   | `im:chat:read` | Read the selected group's name |
   | `contact:contact.base:readonly` | Call the user-information API |
   | `contact:user.base:readonly` | Return name/basic fields from that API |

   The two contact scopes are complementary API-call and field permissions in the current implementation. Core locks/queues use stable IDs; a usable name-based administration UI needs the name lookup combination and an appropriate visibility range. Do not request phone/email, a member-list sweep or all-group-message/history access. `im:message.p2p_msg:readonly` is unused: direct messages are unsupported. “Exempt from permission review” is not exemption from enterprise application publication, availability/data-range configuration or company approval.
3. Subscribe to `im.message.receive_v1`. Prefer the long-connection mode where outbound Feishu HTTPS/WebSocket access is available. It needs no public inbound URL, Verification Token or Encrypt Key. Configure the Feishu-only proxy where necessary; do not apply global proxies that unexpectedly change Infoflow or metric-source traffic.
4. If HTTP callbacks are deliberately chosen instead, use the callback shown for the configured **enterprise application** and the required verification/encryption settings described by that release. Do not reuse the Infoflow per-bot callback. Do not run HTTP and WebSocket receivers concurrently for the same app.
5. Publish the Feishu application through the enterprise's actual approval process. The user or an authorized enterprise administrator must complete review when required. Confirm the version is published, intended members are in scope, and the app bot has been added to the resource group.
6. In Lockbot **Site settings → Message platforms**, register/select the application and its receiving mode; then create a Feishu resource bot with that application and the exact group's `oc_...` Chat ID. Get Chat ID from an approved platform tool or the release's group-association guidance; do not guess it from the group name. The explicit route is not created merely by adding an app bot to a group.
7. Start the application receiver and resource bot as needed. Verify real group query/short lock/release/queue/reminder, correct user/group names, personal Agent binding, webpage links and isolation between two authorized groups. HTTP connectivity, handshake, operation dispatch and accepted reply are different results. Record which was actually observed.

If group or user names cannot be read, check the selected app, granted API/field scopes, application data range, published status and its dedicated proxy. Do not fix a display-name error by changing identity, merging users with matching names or opening arbitrary directory access. Preserve IDs in diagnostics and treat names only as presentation metadata.
