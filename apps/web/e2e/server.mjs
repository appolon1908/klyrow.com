import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, join, normalize } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../dist/', import.meta.url))
const types = { '.css':'text/css', '.html':'text/html', '.js':'text/javascript', '.svg':'image/svg+xml' }
createServer(async (request, response) => {
  const pathname = new URL(request.url || '/', 'http://127.0.0.1').pathname
  const relative = pathname.startsWith('/assets/') ? pathname.slice(1) : 'index.html'
  try {
    const body = await readFile(join(root, normalize(relative)))
    response.writeHead(200, { 'Content-Type':types[extname(relative)] || 'application/octet-stream', 'Cache-Control':'no-store' }); response.end(body)
  } catch { response.writeHead(404); response.end('Not found') }
}).listen(4173, '127.0.0.1')
