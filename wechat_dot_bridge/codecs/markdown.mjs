// Tencent/openclaw-weixin MIT, revision recorded in NOTICE.txt.
import { StreamingMarkdownFilter } from './markdown-filter.ts';
const chunks = [];
let size = 0;
for await (const chunk of process.stdin) {
  size += chunk.length;
  if (size > 262144) process.exit(1);
  chunks.push(chunk);
}
const filter = new StreamingMarkdownFilter();
process.stdout.write(filter.feed(Buffer.concat(chunks).toString('utf8')) + filter.flush());
