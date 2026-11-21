# App-review submissions

QuickReel publishes clips to TikTok, Instagram Reels, and YouTube Shorts. Each
platform gates its publishing API behind a manual app review, and the review is
the long pole: it is an external clock that does not start until the application
is filed and a reviewer can exercise the product. This directory holds the
material each review needs, written from what the software actually does.

The rule for everything here: **do not claim a capability the code does not
have.** A reviewer who finds a permission the product does not use, or a scope
with no matching feature, rejects the application and the clock restarts. The
scope lists in `src/quickreel/oauth.py` are the source of truth; these documents
justify exactly those and nothing more.

## Status

| Platform | Application drafted | Filed | Notes |
| --- | --- | --- | --- |
| TikTok (Content Posting API) | Yes — [tiktok.md](tiktok.md) | No | Needs the demo video recorded against a live deploy |
| Meta / Instagram Reels (Graph API) | Yes — [meta-instagram.md](meta-instagram.md) | No | Needs Business Verification and a screencast |
| Google / YouTube Shorts | Yes — [youtube.md](youtube.md) | No | Needs the OAuth consent-screen verification |

## What the reviewer is given

All three reviews converge on the same two artefacts, so they are written once:

- **A demo account.** A review tenant with a pre-loaded project, so the reviewer
  does not have to supply their own long video to see the feature work. See
  [demo-script.md](demo-script.md).
- **A demo video / screencast.** The recorded walkthrough each form asks for,
  with a shot list that maps each shot to the permission it justifies.

## The permissions we request, and why

These are the only scopes in the codebase. If a review asks for a justification
and the scope is not listed here, it should not be in the request.

### TikTok

| Scope | Why the product needs it | Feature that breaks without it |
| --- | --- | --- |
| `user.info.basic` | Identify which TikTok account a scheduled clip is going to, and show its name in the UI. | The Accounts page cannot label the connection. |
| `video.publish` | Upload and publish a rendered clip to the connected account. | Scheduling and publishing do nothing. |

### Meta / Instagram

| Permission | Why the product needs it | Feature that breaks without it |
| --- | --- | --- |
| `instagram_basic` | Read the connected Instagram Business account's id and username. | The Accounts page cannot label the connection. |
| `instagram_content_publish` | Publish a rendered clip as a Reel. | Scheduling and publishing do nothing. |
| `pages_show_list` | Reach the Facebook Page that the Instagram Business account is linked to. | The app cannot discover which Page owns the IG account, so publishing cannot be addressed. |

### Google / YouTube

| Scope | Why the product needs it | Feature that breaks without it |
| --- | --- | --- |
| `youtube.upload` | Upload a rendered clip as a Short. | Publishing does nothing. |
| `youtube.readonly` | Read the channel id and title for the connection, and read back the video's status after upload. | The Accounts page cannot label the connection, and a failed upload cannot be diagnosed. |

## What we deliberately do not request

Stating this explicitly is part of the submission: reviewers look for
over-scoping, and a shorter list is easier to approve.

- No read access to private messages, followers, or contacts.
- No advertising or audience-insight scopes.
- No scope whose only purpose would be analytics we do not yet ship. The
  performance loop reads the view, like, comment, share and watch-time counters
  of posts this app published itself, which the publish scope already covers;
  it does not request audience or account insights, so no analytics scope is
  requested for it.
- No ability to post without a scheduling action the user took.

## Filing order

1. **TikTok first.** Its review is the most mechanical and the shortest, and the
   demo video recorded for it is reusable for the Meta screencast.
2. **Meta second.** Business Verification is the slow part, so start it as soon
   as the application is filed rather than waiting for the review itself.
3. **Google third.** The consent-screen verification needs a privacy policy at a
   public URL, which is served at `/privacy`. It must be reachable on the
   production origin before this is filed.

## Before filing — checklist

Each platform document ends with its own checklist. These apply to all three:

- [x] Privacy policy served at `/privacy`, with the YouTube API Services and
      Limited Use sections the Google review looks for.
- [x] Terms served at `/terms`.
- [ ] Both reachable at `https://clipviralmoment.com/...` — blocked on the
      domain being registered and attached. Until then the pages are live on the
      deployment's `*.code.run` hostname, and a reviewer given that URL can read
      them, but every form asks for the production origin.
- [ ] The support and privacy mailboxes in `frontend/src/lib/legal.ts` actually
      receive mail. The addresses are `privacy@` and `support@clipviralmoment.com`;
      they need MX records once the domain is registered.
- [ ] `QUICKREEL_PUBLIC_BASE_URL` points at the public origin, so the OAuth
      redirect URI in the form matches what the app sends. It currently points at
      the `*.code.run` hostname and must move to `https://clipviralmoment.com`
      in the same change that attaches the domain.
- [ ] The demo account is seeded and the reviewer credentials are in the form.
- [ ] A real clip has been published end to end to a private test account, so
      the flow is known to work before a reviewer tries it.
