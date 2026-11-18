# Google / YouTube Shorts — OAuth verification

Google's review is the OAuth consent-screen verification, plus a request for the
sensitive/restricted scope. The form asks for the same shape of information as
the others, but the data-handling section is stricter: Google verifies the
privacy policy text and checks the Limited Use disclosure specifically.

## App details

| Field | Value |
| --- | --- |
| App name | QuickReel |
| Support email | `support@clipviralmoment.com` |
| Application home page | `https://clipviralmoment.com` |
| Privacy Policy URL | `https://clipviralmoment.com/privacy` |
| Terms of Service URL | `https://clipviralmoment.com/terms` |
| Authorized domain | `clipviralmoment.com` |
| Redirect URI | `https://clipviralmoment.com/api/v1/oauth/youtube_shorts/callback` |
| Scope type | Sensitive (`youtube.upload`) |

The home page, privacy policy, and authorized domain must all be on the same
domain that is verified in Search Console. Google rejects a submission where the
privacy policy sits on a different host.

## Scopes

| Scope | Justification to paste into the form |
| --- | --- |
| `youtube.upload` | "QuickReel renders short vertical clips from long-form video the user has uploaded and authorised. We upload the rendered clip to the user's own YouTube channel as a Short at the time the user scheduled. Uploads only happen after the user takes an explicit scheduling action." |
| `youtube.readonly` | "We read the user's channel id and title to label the connected account in our interface, and we read back the status of videos uploaded through QuickReel so we can report upload failures to the user instead of failing silently." |

`youtube.readonly` is a sensitive scope and Google will ask why read access is
needed for a feature that is mostly about writing. The answer above is the
reason: an upload that fails after the fact is invisible to the user without the
ability to read the video's processing status, and a scheduling tool that cannot
report that is worse than useless.

## The Limited Use disclosure

Google verifies this wording specifically. The privacy policy at `/privacy`
already contains it, under "YouTube API Services":

> QuickReel uses YouTube API Services. By connecting a YouTube account you also
> agree to the YouTube Terms of Service (https://www.youtube.com/t/terms) and
> acknowledge the Google Privacy Policy (http://www.google.com/policies/privacy).
> Our use of information received from Google APIs adheres to the Google API
> Services User Data Policy, including the Limited Use requirements. We do not
> transfer this data to others except as needed to provide the feature you
> requested, and we do not use it for advertising. You can revoke QuickReel's
> access to your Google account at any time at
> https://myaccount.google.com/permissions.

Confirm this text is live before filing. If the policy is edited, re-check that
this paragraph survives.

## Feature description

> QuickReel turns long-form video into short vertical clips for YouTube Shorts,
> TikTok, and Instagram Reels. A creator uploads a podcast, interview, or stream
> they own or are authorised to use. QuickReel transcribes it, identifies the
> moments most likely to engage a viewer, cuts each to a clean boundary, renders
> it to 1080×1920 with burned captions, and scores it. The creator reviews the
> clips and decides what to publish.
>
> To publish to YouTube, the creator connects their channel once through
> Google's consent screen. When the creator schedules a clip, QuickReel uploads
> the rendered MP4 to the creator's own channel and, if the upload fails, reads
> the video's status to explain why. The creator can revoke access at any time
> from their Google account permissions, which stops all further uploads.

## Demo video

Use [demo-script.md](demo-script.md). Google's reviewer specifically wants to
see the **Google consent screen** and the **scope list** on it, then the upload
succeeding. Shots 5, 6, 7, and 9 cover this; the consent screen in shot 5 is
Google's own, which is what the verification is checking.

Google also requires the demo to show the app's home page and how the OAuth
client is used in context, which shots 1 and 2 cover.

## Verification prerequisites

Google will not verify until:

- [ ] The authorized domain is verified in Google Search Console.
- [ ] The privacy policy is live at the URL above, on the authorized domain, and
      contains the Limited Use disclosure and the revocation link
      (`https://myaccount.google.com/permissions`).
- [ ] The app's home page is live and describes the product (the landing page).
- [ ] A demo video is provided showing the consent screen and the feature.
- [ ] `QUICKREEL_YOUTUBE_SHORTS_CLIENT_ID` and `QUICKREEL_YOUTUBE_SHORTS_CLIENT_SECRET` set.
- [ ] `QUICKREEL_PUBLIC_BASE_URL` matches the redirect URI.

## After approval

The YouTube adapter is not implemented yet. Beyond publishing, YouTube has a
second integration that is already built and does not need this verification:
the **WebSub ingestion trigger** in `src/quickreel/triggers/`, which ingests a
subscribed channel's new uploads for clipping. That uses the public PubSubHubbub
hub and no OAuth scope, so it is independent of this review.

Note the interaction for the form: the ingestion trigger downloads videos from
channels the user has authorised. This is a different permission basis from
uploading, and the form should not conflate them. Uploading uses
`youtube.upload` on the user's own channel; ingesting a channel's videos relies
on the user confirming they are authorised to process that channel, which the
subscription records in `authorized_by` and `license_note`.
