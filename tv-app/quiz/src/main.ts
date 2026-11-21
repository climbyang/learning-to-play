import { ApiClient } from "./api";
import { App } from "./app";
import { QUIZ_TOKEN, QUIZ_URL } from "./config";

const root = document.getElementById("app");
if (!root) throw new Error("#app missing");

const app = new App(root, new ApiClient({ baseUrl: QUIZ_URL, token: QUIZ_TOKEN }));

document.addEventListener("keydown", (e) => {
  if (app.handleKey(e)) e.preventDefault();
});
// webOS fires this when the app is launched again while already running.
document.addEventListener("webOSRelaunch", () => app.refresh());
app.start(document);
