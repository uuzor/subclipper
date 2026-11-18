import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import Shell from "./components/Shell";
import RequireAuth from "./components/RequireAuth";
import Landing from "./routes/Landing";
import Workbench from "./routes/Workbench";
import Project from "./routes/Project";
import Accounts from "./routes/Accounts";
import Channels from "./routes/Channels";
import SignIn from "./routes/SignIn";
import { Privacy, Terms } from "./routes/Legal";
import NotFound from "./routes/NotFound";
import { AuthProvider } from "./lib/auth";
import "./styles/global.css";

// The public routes (landing page, sign-in, legal pages) sit outside the guard.
// Everything that reads or writes a project is behind it, so an unauthenticated
// visitor is redirected before a request can 401.
const router = createBrowserRouter([
  {
    path: "/",
    element: (
      <AuthProvider>
        <Shell />
      </AuthProvider>
    ),
    children: [
      { index: true, element: <Landing /> },
      { path: "signin", element: <SignIn /> },
      { path: "privacy", element: <Privacy /> },
      { path: "terms", element: <Terms /> },
      {
        element: <RequireAuth />,
        children: [
          { path: "workbench", element: <Workbench /> },
          { path: "accounts", element: <Accounts /> },
          { path: "channels", element: <Channels /> },
          { path: "projects/:projectId", element: <Project /> },
        ],
      },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
