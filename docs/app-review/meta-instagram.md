# Meta / Instagram Reels (Graph API) — application

Text to paste into the Meta for Developers App Review form, with the fields
named as the form names them. Meta's review is the slowest of the three because
Business Verification runs in parallel and is a prerequisite for most
permissions, so file early and start verification immediately.

## App details

| Field | Value |
| --- | --- |
| App name | QuickReel |
| App domain | `clipviralmoment.com` |
| Website URL | `https://clipviralmoment.com` |
| Privacy Policy URL | `https://clipviralmoment.com/privacy` |
| Terms of Service URL | `https://clipviralmoment.com/terms` |
| Data Deletion Instructions URL | `https://clipviralmoment.com/privacy` (see note below) |
| OAuth Redirect URI | `https://clipviralmoment.com/api/v1/oauth/instagram_reels/callback` |
| Category | Business and pages / Content publishing |
| Business Verification | Required — start it now |

### On the data-deletion URL

Meta requires a URL describing how a user deletes their data. Until a dedicated
page exists, `/privacy` covers it under "Your rights". A dedicated
`/data-deletion` route is a small addition and worth making before filing, so
the form points at a page that answers the question directly rather than making
the reviewer hunt for it.

## Permissions

Request exactly these three.

| Permission | Justification to paste into the form |
| --- | --- |
| `instagram_basic` | "We read the connected Instagram Business account's id and username so the creator can see which account a scheduled Reel will be published to, and confirm the destination before publishing." |
| `instagram_content_publish` | "QuickReel renders short vertical clips from long-form video the creator has uploaded and authorised. We use this permission to publish a rendered clip as a Reel to the creator's own Instagram Business account at the time the creator scheduled. Nothing is published without the creator scheduling it." |
| `pages_show_list` | "The Instagram Business account is linked to a Facebook Page. We read the list of Pages the creator administers so we can identify the Page that owns the Instagram Business account and address the publish call correctly." |

## Feature description (the "how will you use this permission" box)

> QuickReel turns long-form video into short vertical clips for Reels, TikTok,
> and YouTube Shorts. A creator uploads a podcast, interview, or stream they own
> or are authorised to use. QuickReel transcribes it, finds the moments most
> likely to engage a viewer, cuts each to a clean boundary, renders it to
> 1080×1920 with burned captions, and scores it. The creator reviews the clips
> and decides what to publish.
>
> To publish to Instagram, the creator connects their Instagram Business account
> once through Facebook Login. When the creator schedules a Reel, QuickReel
> creates a media container from the rendered MP4 and publishes it to the
> creator's own account at the scheduled time. The creator can disconnect at any
> time, which stops all further publishing.
>
> We request the minimum permissions for this: basic account identification, the
> ability to publish content the creator scheduled, and the Page list needed to
> address the publish call. We do not request analytics, messaging, or
> advertising permissions, and we do not read content the creator did not submit
> through QuickReel.

## Review notes for the screencast

Meta reviewers follow a screencast shot by shot and reject if a permission is
not visibly exercised. Use [demo-script.md](demo-script.md); the shots that map
to this review are:

| Permission | Shot that demonstrates it |
| --- | --- |
| `instagram_basic` | Shot 6 — the connected account shows the Instagram username. |
| `pages_show_list` | Shot 5 — the Facebook Login consent screen; and shot 6, where the Page that owns the IG account is resolved. |
| `instagram_content_publish` | Shots 7 and 9 — scheduling a Reel and seeing it published. |

The screencast must show the **Facebook Login consent screen itself**, with the
three permissions visible. A screencast that starts after consent is a common
rejection reason.

## Test user instructions (the "how to test" box)

> A review account is provided:
>
> 1. Sign in at `https://clipviralmoment.com` with the credentials in the
>    submission.
> 2. Open the pre-loaded project "Demo: review walkthrough". It contains three
>    scored clips; one is already rendered to vertical with captions.
> 3. Open **Accounts**, click **Connect** next to Instagram, and approve the
>    Facebook Login consent screen. The connected account will appear with its
>    username.
> 4. On the rendered clip, click **Schedule**, choose the connected account, and
>    set a time two minutes ahead.
> 5. The post appears in the list as scheduled and publishes at that time to the
>    private test account. Its status updates to published.
> 6. Disconnect the account from **Accounts** to confirm revocation works.

## Data handling answers

| Question | Answer |
| --- | --- |
| Do you use data for advertising? | No. |
| Do you sell data? | No. |
| Do you transfer data to third parties? | No, other than to the platform the creator asked us to publish to. |
| Retention | Content until deleted; credentials until the account is disconnected or deleted. |
| Deletion | Deleting a project removes its media and clips; deleting the account removes connected credentials. See the privacy policy. |

## Before filing — checklist

- [ ] Meta app created and linked to the correct Business.
- [ ] **Business Verification started** (it gates the review).
- [ ] App domain and redirect URI set and matching `QUICKREEL_PUBLIC_BASE_URL`.
- [ ] Privacy policy and terms live and reachable.
- [ ] A data-deletion page or an explicit section the URL points to.
- [ ] Instagram account used for testing is a **Business** account linked to a
      Facebook Page (a personal or Creator account cannot publish via this API).
- [ ] Screencast recorded, showing the consent screen, under 5 minutes.
- [ ] Test-user instructions pasted into the form.
- [ ] `QUICKREEL_INSTAGRAM_REELS_CLIENT_ID` and `QUICKREEL_INSTAGRAM_REELS_CLIENT_SECRET` set in the
      deployment.

## After approval

As with TikTok, the live adapter is not implemented yet. Meta also requires the
app to be switched from Development to **Live** mode before third-party users
can connect, which is a separate step from passing review. The publish adapter
lands in `src/quickreel/publishing/` and registers in `platforms.py`.
