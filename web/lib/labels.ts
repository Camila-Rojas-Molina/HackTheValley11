// Every visible label, disclaimer and badge comes from contracts/labels.json (C6), the single
// source the docs lint and the AI checker also read. Never copy its strings into components.
import labels from "@contracts/labels.json";

export { labels };
export type Labels = typeof labels;
