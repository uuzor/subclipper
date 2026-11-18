/**
 * Legal pages: privacy policy and terms of service.
 *
 * Kept as data rather than JSX so the text can be reviewed, diffed, and reused
 * (the platform app-review forms ask for the same disclosures verbatim) without
 * reading through markup.
 *
 * These are a starting point written to match what the software actually does.
 * They are not legal advice: have a lawyer review them before relying on them,
 * and update the effective date and contact address first.
 */

export const COMPANY = {
  product: "QuickReel",
  domain: "clipviralmoment.com",
  contactEmail: "privacy@clipviralmoment.com",
  supportEmail: "support@clipviralmoment.com",
  effectiveDate: "17 November 2026",
};

export interface LegalSection {
  heading: string;
  paragraphs: string[];
  bullets?: string[];
}

export const PRIVACY_SECTIONS: LegalSection[] = [
  {
    heading: "What this policy covers",
    paragraphs: [
      `This policy explains how ${COMPANY.product} ("we") handles personal data when you use ${COMPANY.domain}. It covers the videos you upload, the accounts you connect, and the analytics we collect about clips you publish.`,
      "Where we process data on behalf of a business customer, that customer is the controller and we are the processor.",
    ],
  },
  {
    heading: "Data we collect",
    paragraphs: ["We collect the following categories of data:"],
    bullets: [
      "Account data: your email address and the accounts you connect to publish clips.",
      "Content: videos you upload or import, the transcripts we derive from them, and the clips we render.",
      "Provenance: the licence note and authorising person you record against a project, so a clip can be traced to its basis for republication.",
      "Connected-platform credentials: OAuth access and refresh tokens for the publishing accounts you connect.",
      "Usage and performance data: clip scores, publish attempts, and engagement metrics (views, likes, comments, shares, retention) for clips you publish.",
      "Technical data: IP address, request logs, and error traces, kept for security and debugging.",
    ],
  },
  {
    heading: "How we use connected-platform data",
    paragraphs: [
      "When you connect a platform account, we use the access we are granted only to publish the clips you schedule and to read back the performance of posts you made through us. We request the narrowest scopes that allow this: the ability to upload and publish, plus basic account identification.",
      "We do not read your private messages, your followers, your advertising data, or any content you did not submit through QuickReel. We do not post anything without an action you took to schedule it.",
      "We do not sell connected-platform data, and we do not use it to train models for other customers.",
    ],
  },
  {
    heading: "YouTube API Services",
    paragraphs: [
      "QuickReel uses YouTube API Services. By connecting a YouTube account you also agree to the YouTube Terms of Service (https://www.youtube.com/t/terms) and acknowledge the Google Privacy Policy (http://www.google.com/policies/privacy).",
      "Our use of information received from Google APIs adheres to the Google API Services User Data Policy, including the Limited Use requirements. We do not transfer this data to others except as needed to provide the feature you requested, and we do not use it for advertising.",
      "You can revoke QuickReel's access to your Google account at any time at https://myaccount.google.com/permissions. Revoking access stops all further publishing through QuickReel.",
    ],
  },
  {
    heading: "TikTok and Meta",
    paragraphs: [
      "Publishing to TikTok uses the TikTok Content Posting API and is subject to TikTok's terms and privacy policy. Publishing to Instagram Reels uses the Meta Graph API and is subject to Meta's terms and privacy policy.",
      "Tokens for these platforms are stored so that scheduled posts can be published later, and are used only for that purpose.",
    ],
  },
  {
    heading: "Storage and retention",
    paragraphs: [
      "Source videos, transcripts, rendered clips, and credentials are stored on our infrastructure. Credentials are stored separately from published content and are never returned to a browser or written to application logs.",
      "We keep your content until you delete it or delete your account. Deleting a project removes its media, transcript, and clips. Deleting an account removes the associated publishing credentials. We may retain logs and billing records for a limited period where the law requires it.",
    ],
  },
  {
    heading: "Security",
    paragraphs: [
      "We use HTTPS for all traffic, validate all uploaded input, and restrict what a connected account can do to the scopes you granted. No system is perfectly secure: if you believe your account has been compromised, contact us immediately.",
    ],
  },
  {
    heading: "Your rights",
    paragraphs: [
      "Depending on where you live, you may have the right to access, correct, export, or delete your personal data, and to object to certain processing. To make a request, email us at " +
        COMPANY.contactEmail +
        ". We will respond within the period required by applicable law.",
    ],
  },
  {
    heading: "Children",
    paragraphs: [
      "QuickReel is not intended for anyone under 16, and we do not knowingly collect data from children.",
    ],
  },
  {
    heading: "Changes",
    paragraphs: [
      "We may update this policy. If we make a material change, we will notify you in the product or by email before it takes effect.",
    ],
  },
  {
    heading: "Contact",
    paragraphs: [
      `Questions about this policy: ${COMPANY.contactEmail}. Product support: ${COMPANY.supportEmail}.`,
    ],
  },
];

export const TERMS_SECTIONS: LegalSection[] = [
  {
    heading: "Agreement",
    paragraphs: [
      `These terms govern your use of ${COMPANY.product} at ${COMPANY.domain}. By using the service you agree to them. If you are using it for an organisation, you confirm you have authority to bind that organisation.`,
    ],
  },
  {
    heading: "Your content and your rights",
    paragraphs: [
      "You keep ownership of the videos you upload and the clips we render from them. You grant us the licence needed to process that content: to store it, transcribe and analyse it, render clips, and publish clips you schedule to accounts you connected.",
      "You are responsible for having the rights to the material you ingest. QuickReel does not grant you any rights to content you did not create, and downloaded platform content remains subject to that platform's terms of service.",
      "You must not use QuickReel to ingest or republish content you do not own or have permission to use, to harass anyone, to publish unlawful material, or to misrepresent someone's words by clipping them out of context.",
    ],
  },
  {
    heading: "Provenance",
    paragraphs: [
      "QuickReel records the licence note and authorising person you provide for each project. This record exists to make your rights auditable. It is your responsibility to ensure what you record is accurate.",
    ],
  },
  {
    heading: "Connected platform accounts",
    paragraphs: [
      "When you connect an account, you authorise us to publish on your behalf and to read back the performance of posts made through QuickReel. You must only connect accounts you are entitled to act for.",
      "Your use of a connected platform is also governed by that platform's terms. If a platform rejects a post, suspends an account, or changes its API, we are not responsible for the consequences, though we will report the failure and stop retrying when a retry cannot succeed.",
    ],
  },
  {
    heading: "Viral potential is a prediction",
    paragraphs: [
      "Clip scores are predictions of engagement, not guarantees of performance. A score is an estimate produced by a model, and we make no promise about views, reach, or revenue.",
    ],
  },
  {
    heading: "Acceptable use and suspension",
    paragraphs: [
      "We may suspend or terminate access if you breach these terms, if your use threatens the security or stability of the service, or if we are required to by law. Where practical, we will tell you why.",
    ],
  },
  {
    heading: "Availability and changes",
    paragraphs: [
      "The service is provided as is, without warranties of any kind. We may change or discontinue features. We aim to give notice of material changes, particularly those affecting connected accounts.",
    ],
  },
  {
    heading: "Limitation of liability",
    paragraphs: [
      "To the maximum extent permitted by law, we are not liable for indirect, incidental, or consequential damages, or for lost profits, lost reach, or lost content. Nothing in these terms excludes liability that cannot lawfully be excluded.",
    ],
  },
  {
    heading: "Governing law",
    paragraphs: [
      "These terms are governed by the laws of the jurisdiction in which we are established, without regard to conflict-of-law rules. [Set the governing jurisdiction before publishing.]",
    ],
  },
  {
    heading: "Contact",
    paragraphs: [`Questions about these terms: ${COMPANY.supportEmail}.`],
  },
];
