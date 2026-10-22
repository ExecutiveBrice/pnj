// Run with node --test tests/mobile_offline.test.cjs (also exercised by pytest
// when Node is installed). Browser storage itself is verified in the UI.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');

const campaignA = 'a'.repeat(32), campaignB = 'b'.repeat(32);
const item = {id: '1'.repeat(32), title: 'La forêt', audio_url: `/studio/audio/${'1'.repeat(32)}.wav`};
const catalog = campaign => ({campaign_id: campaign, stories: [item]});

function fixture() {
  let response = new Response(new Blob(['RIFF', 'test audio']), {headers: {'Content-Type': 'audio/wav'}});
  const revoked = [], stores = {audio: new Map(), catalogs: new Map(), settings: new Map()};
  const context = vm.createContext({
    Blob, Response, Error, Map, navigator: {},
    URL: {createObjectURL: () => 'blob:local-audio', revokeObjectURL: url => revoked.push(url)},
    IDBKeyRange: {bound: (from, to) => ({from, to})},
    fetch: async () => { if (response instanceof Error) throw response; return response.clone(); },
  });
  const EchoOffline = vm.runInContext(readFileSync(resolve(__dirname, '../src/npc/static/mobile-offline.js'), 'utf8') + '\nEchoOffline;', context);
  // Substitute only the IndexedDB adapter, keeping library behavior intact.
  const offline = Object.assign(Object.create(EchoOffline.prototype), {
    campaign: null, saved: new Map(), urls: new Map(),
    transaction: async (names, mode, action) => action({objectStore: name => ({
      get: key => ({result: stores[name].get(key)}),
      getAll: range => ({result: [...stores[name]].filter(([key]) => key >= range.from && key <= range.to).map(([, value]) => value)}),
      put: (value, key) => stores[name].set(key, value),
      delete: key => stores[name].delete(key),
    })})?.result,
  });
  return {offline, stores, revoked, setResponse: value => response = value};
}

test('download survives reopening without any network and can be removed', async () => {
  const f = fixture();
  await f.offline.select(catalog(campaignA));
  await f.offline.save(item);
  assert.equal(f.offline.url(item), 'blob:local-audio');
  f.setResponse(new Error('PC switched off'));
  // Simulate a new page with the same persisted database.
  f.offline.campaign = null;
  f.offline.saved.clear();
  assert.equal((await f.offline.restore()).campaign_id, campaignA);
  assert.equal(await f.offline.saved.get(item.id).blob.text(), 'RIFFtest audio');
  assert.equal(f.offline.url(item), 'blob:local-audio');
  await f.offline.remove(item);
  assert.equal(f.stores.audio.size, 0);
  assert.equal(f.offline.url(item), '');
  assert.equal(f.revoked.length, 2);
});

test('campaign switch never exposes another campaign audio, including colliding ids', async () => {
  const f = fixture();
  await f.offline.select(catalog(campaignA));
  await f.offline.save(item);
  f.offline.url(item);
  await f.offline.select(catalog(campaignB));
  assert.equal(f.offline.saved.size, 0);
  assert.equal(f.offline.url(item), '');
  assert.equal(f.revoked.length, 1);
  await f.offline.select(catalog(campaignA));
  assert.equal(f.offline.saved.size, 1);
});

test('partial, unauthorized, invalid and interrupted downloads never become offline copies', async () => {
  for (const response of [
    new Response('RIFFpartial', {status: 206, headers: {'Content-Type': 'audio/wav'}}),
    new Response('Unauthorized', {status: 401}),
    new Response('<html>Error</html>', {headers: {'Content-Type': 'text/html'}}),
    new Response('invalid', {headers: {'Content-Type': 'audio/wav'}}),
    new Error('Connection lost'),
  ]) {
    const f = fixture();
    await f.offline.select(catalog(campaignA));
    f.setResponse(response);
    await assert.rejects(f.offline.save(item));
    assert.equal(f.stores.audio.size, 0);
    assert.equal(f.offline.saved.size, 0);
  }
});

test('storage quota failure leaves the previous offline copy intact', async () => {
  const f = fixture();
  await f.offline.select(catalog(campaignA));
  await f.offline.save(item);
  const previous = f.offline.saved.get(item.id);
  f.offline.transaction = async () => { throw new Error('QuotaExceededError'); };
  await assert.rejects(f.offline.save(item));
  assert.equal(f.offline.saved.get(item.id), previous);
  assert.equal(f.stores.audio.size, 1);
});

test('a download finishing after a campaign switch stays in its original campaign', async () => {
  const f = fixture();
  await f.offline.select(catalog(campaignA));
  let release;
  const transaction = f.offline.transaction;
  f.offline.transaction = async (names, mode, action) => {
    if (names[0] === 'audio' && mode === 'readwrite') await new Promise(resolve => release = resolve);
    return transaction(names, mode, action);
  };
  const saving = f.offline.save(item);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  await f.offline.select(catalog(campaignB));
  release();
  await saving;
  assert.equal(f.offline.saved.size, 0);
  assert.equal(f.stores.audio.has(`${campaignA}:${item.id}`), true);
  assert.equal(f.stores.audio.has(`${campaignB}:${item.id}`), false);
});
