import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition} from '@remotion/renderer';
import path from 'node:path';
import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const root = path.resolve(__dirname, '..');

const jobPath = process.argv[2];
if (!jobPath) {
  console.error('Usage: npm run render-job -- <job.json>');
  process.exit(1);
}

const absoluteJobPath = path.resolve(process.cwd(), jobPath);
const job = JSON.parse(await fs.readFile(absoluteJobPath, 'utf8'));
const inputProps = job.props ?? job;
const outputLocation = path.resolve(path.dirname(absoluteJobPath), job.output ?? 'render.mp4');

await fs.mkdir(path.dirname(outputLocation), {recursive: true});

const serveUrl = await bundle({
  entryPoint: path.join(root, 'src', 'index.tsx'),
});

const composition = await selectComposition({
  serveUrl,
  id: 'MotionGraphic',
  inputProps,
});

await renderMedia({
  composition,
  serveUrl,
  codec: 'h264',
  outputLocation,
  inputProps,
  overwrite: true,
  onProgress: ({progress}) => {
    const percent = Math.round(progress * 100);
    if (percent % 10 === 0) {
      console.log(`progress ${percent}%`);
    }
  },
});

console.log(outputLocation);
