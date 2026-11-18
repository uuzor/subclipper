import { Link } from "react-router-dom";
import { COMPANY, PRIVACY_SECTIONS, TERMS_SECTIONS, type LegalSection } from "../lib/legal";
import "./legal.css";

export function LegalPage({
  title,
  intro,
  sections,
}: {
  title: string;
  intro: string;
  sections: LegalSection[];
}) {
  return (
    <div className="shell legal">
      <header className="legal-head">
        <p className="eyebrow mono">{COMPANY.product}</p>
        <h1>{title}</h1>
        <p className="legal-meta mono">
          Effective {COMPANY.effectiveDate} · {COMPANY.domain}
        </p>
        <p className="lede">{intro}</p>
      </header>

      <nav className="legal-nav" aria-label="Legal documents">
        <Link to="/privacy">Privacy policy</Link>
        <Link to="/terms">Terms of service</Link>
      </nav>

      <div className="legal-body">
        {sections.map((section) => (
          <section key={section.heading} className="legal-section">
            <h2>{section.heading}</h2>
            {section.paragraphs.map((paragraph) => (
              <p key={paragraph}>{paragraph}</p>
            ))}
            {section.bullets && (
              <ul>
                {section.bullets.map((bullet) => (
                  <li key={bullet}>{bullet}</li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>

      <p className="legal-foot mono">
        Questions? <a href={`mailto:${COMPANY.contactEmail}`}>{COMPANY.contactEmail}</a>
      </p>
    </div>
  );
}

export function Privacy() {
  return (
    <LegalPage
      title="Privacy policy"
      intro={`How ${COMPANY.product} collects, uses, and protects your data, including data from connected platform accounts.`}
      sections={PRIVACY_SECTIONS}
    />
  );
}

export function Terms() {
  return (
    <LegalPage
      title="Terms of service"
      intro={`The terms under which you may use ${COMPANY.product}.`}
      sections={TERMS_SECTIONS}
    />
  );
}
