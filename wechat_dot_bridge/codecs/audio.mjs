// Local binary-only helper. silk-wasm is MIT; no networking or filesystem inputs.
import { encode, decode, getDuration } from 'silk-wasm';
try {
  const chunks = []; let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 20 * 1024 * 1024) throw new Error();
    chunks.push(chunk);
  }
  const input = Buffer.concat(chunks);
  let result;
  if (process.argv[2] === 'encode') {
    if (input.length > 24000 * 2 * 300) throw new Error();
    result = await encode(input, 24000);
  } else if (process.argv[2] === 'decode') {
    if (getDuration(input) > 300000) throw new Error();
    result = await decode(input, 24000);
  } else throw new Error();
  process.stdout.write(result.data);
} catch {
  process.stderr.write('audio_conversion_failed\n');
  process.exitCode = 1;
}
