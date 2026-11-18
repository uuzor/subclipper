import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import Shell from "./components/Shell";
import Landing from "./routes/Landing";
import Workbench from "./routes/Workbench";
import Project from "./routes/Project";
import Accounts from "./routes/Accounts";
import Channels from "./routes/Channels";
import { Privacy, Terms } from "./routes/Legal";
import NotFound from "./routes/NotFound";
import "./styles/global.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <Shell />,
    children: [
      { index: true, element: <Landing /> },
      { path: "workbench", element: <Workbench /> },
      { path: "accounts", element: <Accounts /> },
      { path: "channels", element: <Channels /> },
      { path: "privacy", element: <Privacy /> },
      { path: "terms", element: <Terms /> },
      { path: "projects/:projectId", element: <Project /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
