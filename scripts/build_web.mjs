import {build} from "esbuild-wasm";
import {fileURLToPath} from "node:url";

process.chdir(fileURLToPath(new URL("../", import.meta.url)));
await build({
  entryPoints:["frontend/src/conversation.tsx"],
  outfile:"web/react-conversation.js",
  bundle:true,
  format:"iife",
  globalName:"LabConversation",
  target:["es2020"],
  minify:true,
  legalComments:"linked",
  define:{"process.env.NODE_ENV":'"production"'},
});
