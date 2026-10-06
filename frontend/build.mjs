import { mkdir, rm, cp, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const root = new URL('.', import.meta.url);
const dist = new URL('./dist/', root);
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
for (const file of ['index.html', 'app.js', 'styles.css']) await cp(new URL(`./${file}`, root), new URL(`./dist/${file}`, root));
await writeFile(new URL('./dist/build-manifest.json', root), JSON.stringify({ product: 'SmartSwap', files: ['index.html', 'app.js', 'styles.css'] }, null, 2));
console.log(`Built SmartSwap frontend to ${join(process.cwd(), 'dist')}`);
