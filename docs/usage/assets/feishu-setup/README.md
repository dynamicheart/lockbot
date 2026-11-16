# Feishu setup screenshots

The initial six images were captured on 2026-10-29 from the official Feishu developer console using its logged-in browser UI; the robot capability card was added from a read-only visit on 2026-10-30. These are native browser screenshot clips, not pixel edits or recreated screens. No full-page originals are included in the repository.

| File | Console area | Excluded from capture |
| --- | --- | --- |
| `create-application.jpg` | Empty application-name/description/icon fields | Enterprise notice, account header, application list and address bar |
| `console-navigation.jpg` | Generic configuration navigation | Application credential area, enterprise/account header and address bar |
| `bot-capability.jpg` | Generic robot capability card on 2026-10-30; already enabled, so the button says Configure | Application/enterprise/account identity, credentials and address bar; the small chat illustration belongs to the official card, not a captured user conversation |
| `permissions.jpg` | Five enabled application permissions | Application/enterprise identity, personal data and address bar |
| `websocket.jpg` | Long-connection explanation | Application/enterprise header and address bar |
| `event-subscription.jpg` | `im.message.receive_v1` subscription | Application/enterprise header and address bar |
| `release-entry.jpg` | Version-management heading and generic release notice | Publisher, version history, enterprise identity and address bar |

The empty creation dialog was canceled and confirmed hidden. Capture did not create an application, add scopes, publish a version, reveal credentials, send a message or change a receiver. The screenshots show existing console configuration and read-only form inspection, not proof of a fresh tenant's end-to-end setup.

Permission names and menu locations are observations of the official UI. Approval and availability still depend on each enterprise. The guide distinguishes core resource commands from optional name/group-display capabilities, and API-call permission from field permission.

The 2026-10-30 follow-up reviewed all six existing clips and captured only the robot capability card from the authorized test enterprise. No Configure/Add button was clicked. The version-management page was inspected read-only; no version was created, permission changed, publication/approval submitted or message sent. A release-entry image is not a record of a new approval, and the repository does not contain a version form, member-range selection or approval-flow screenshot. The guide lists these remaining evidence boundaries explicitly. No corporate-enterprise configuration was accessed.

## LockBot console examples

On 2026-11-15, a local LockBot frontend was rendered with synthetic API responses for two additional crops. `lockbot-applications.png` shows the applications table with a synthetic receiver state. `lockbot-group-route.png` shows the creation form with a fictional enterprise application selected and an empty Chat ID field. These images contain no real credentials, groups, or identities. A real receiver handshake and message test were outside this capture.
