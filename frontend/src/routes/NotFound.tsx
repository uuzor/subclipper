import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="shell" style={{ paddingTop: "var(--s9)", display: "grid", gap: "var(--s4)", justifyItems: "start" }}>
      <h1 style={{ fontSize: "2.4rem" }}>Nothing at this address.</h1>
      <p style={{ color: "var(--ink-soft)" }}>
        The page moved, or the project was deleted.
      </p>
      <Link to="/workbench" className="btn btn-primary">
        Open the workbench
      </Link>
    </div>
  );
}
