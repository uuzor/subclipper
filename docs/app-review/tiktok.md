# TikTok Content Posting API — application

This is the text to paste into the TikTok for Developers application form, with
the fields named as the form names them. Fill the bracketed values at filing
time; do not file with a placeholder left in.

## App details

| Field | Value |
| --- | --- |
| App name | QuickReel |
| App URL / website | `https://clipviralmoment.com` |
| Terms of Service URL | `https://clipviralmoment.com/terms` |
| Privacy Policy URL | `https://clipviralmoment.com/privacy` |
| Redirect URI | `https://clipviralmoment.com/api/v1/oauth/tiktok/callback` |
| Category | Video editing / Content creation |
| Platforms | Web |
| Contact email | `support@clipviralmoment.com` |

The redirect URI must match `QUICKREEL_PUBLIC_BASE_URL` in the deployed
configuration exactly. TikTok rejects the whole application for a mismatch, and
it is the most common reason a first submission bounces.

## Products requested

**Content Posting API** — the only product. Login Kit is not requested on its
own; the authorization code flow that the Content Posting API needs is the only
login surface we use.

## Scopes

Request exactly these two. Nothing else.

| Scope | Justification to paste into the form |
| --- | --- |
| `user.info.basic` | "We use the basic profile to identify which TikTok account a scheduled clip will be published to, so the user can see and confirm the destination in our interface before publishing." |
| `video.publish` | "QuickReel renders short vertical clips from long-form video the user has uploaded and authorised. We use this scope to upload the rendered clip and publish it to the user's own account at the time the user has scheduled. Publishing only ever happens after the user takes an explicit scheduling action." |

If the form asks whether the app uses the **Direct Post** or **Upload** endpoint,
the honest answer is Direct Post: the user schedules a post and QuickReel
publishes it at that time without a further TikTok-side confirmation step.

## How the feature works (the "describe your use case" box)

> QuickReel turns long-form video into short vertical clips. A creator uploads a
> podcast, interview, or stream they have the rights to, and QuickReel transcribes
> it, identifies the moments most likely to engage a viewer, cuts each to a clean
> boundary, renders it to 1080×1920 with captions, and scores it. The creator
> reviews the clips in a web interface and chooses which to publish.
>
> To publish, the creator connects their TikTok account once through TikTok's own
> consent screen. From then on, when the creator schedules a clip, QuickReel
> uploads the rendered MP4 and publishes it to that creator's account at the
> scheduled time. The creator can disconnect the account at any time from the
> same screen, which stops all further publishing.
>
> QuickReel does not post anything without the creator scheduling it. It does not
> read the creator's messages, followers, or analytics. It requests only
> `user.info.basic` (to label the connection) and `video.publish` (to publish the
> clip the creator scheduled).

## Data handling answers

TikTok's form asks several data-handling questions. Answer them as follows, and
keep the answers consistent with the privacy policy.

| Question | Answer |
| --- | --- |
| Where is data stored? | On our own infrastructure, with credentials stored separately from published content. |
| Do you share data with third parties? | No. We do not sell or share TikTok data. |
| How long is data retained? | Content until the user deletes it; credentials until the account is disconnected or the user's account is deleted. |
| Do you use the data for advertising? | No. |
| How do users delete their data? | Deleting a project removes its media and clips; deleting the account removes connected credentials. Requests can also be sent to `privacy@clipviralmoment.com`. |

## Demo video

Record against the shot list in [demo-script.md](demo-script.md). The shots that
matter most for this form are 5, 6, 7, and 9: consent, the labelled connection,
the explicit scheduling action, and the published post.

## Review credentials

Provide the demo account from [demo-script.md](demo-script.md). The reviewer
must be able to reach a scheduled post without uploading their own video.

## Before filing — checklist

- [ ] App created in the TikTok developer portal under the correct organisation.
- [ ] Redirect URI set and matching `QUICKREEL_PUBLIC_BASE_URL`.
- [ ] Privacy policy and terms reachable at the URLs above (not a 404).
- [ ] Both scope justifications pasted verbatim.
- [ ] Demo video recorded, under 5 minutes, captioned.
- [ ] Demo account seeded with a processed project and a scheduled post.
- [ ] `QUICKREEL_TIKTOK_CLIENT_ID` and `QUICKREEL_TIKTOK_CLIENT_SECRET` set in the
      deployment (they are read from the environment; see `.env.example`).

## After approval

The publishing adapter does not ship yet. `src/quickreel/publishing/platforms.py`
has the registration hook commented out; landing the TikTok adapter means writing
a `PlatformAdapter` that calls the Content Posting API and registering it there.
Until then, `QUICKREEL_PUBLISHING_MODE=mock` is the only working mode, and the
production guard refuses to start with `mock` in production — which is deliberate,
so an approved app cannot silently publish to a stub.
