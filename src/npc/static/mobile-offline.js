'use strict';

// Audio and its metadata commit together, so an interrupted download is never
// advertised as available offline. Each campaign keeps its own library.
class EchoOffline {
  constructor() {
    this.campaign = null;
    this.saved = new Map();
    this.urls = new Map();
    this.db = new Promise((resolve, reject) => {
      const request = indexedDB.open('echo-offline', 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore('catalogs');
        request.result.createObjectStore('audio');
        request.result.createObjectStore('settings');
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('Stockage bloqué par un autre onglet.'));
    });
    // The page remains usable when storage is disabled by the browser.
    this.db.catch(() => {});
  }

  async transaction(stores, mode, action) {
    const db = await this.db;
    return new Promise((resolve, reject) => {
      const tx = db.transaction(stores, mode);
      const result = action(tx);
      tx.oncomplete = () => resolve(result?.result);
      tx.onabort = tx.onerror = () => reject(tx.error || new Error('Stockage indisponible.'));
    });
  }

  async restore() {
    const campaign = await this.transaction(['settings'], 'readonly', tx => tx.objectStore('settings').get('lastCampaign'));
    if (!campaign) return null;
    const catalog = await this.transaction(['catalogs'], 'readonly', tx => tx.objectStore('catalogs').get(campaign));
    if (catalog) await this.select(catalog, false);
    return catalog;
  }

  async select(catalog, persist = true) {
    const campaign = catalog.campaign_id;
    if (!/^[a-f0-9]{32}$/.test(campaign)) throw new Error('Campagne non identifiée.');
    if (this.campaign !== campaign) {
      for (const url of this.urls.values()) URL.revokeObjectURL(url);
      this.urls.clear();
      this.saved.clear();
      this.campaign = campaign;
      // Prefix range avoids loading the WAVs of other campaigns into memory.
      const records = await this.transaction(['audio'], 'readonly', tx => tx.objectStore('audio').getAll(
        IDBKeyRange.bound(`${campaign}:`, `${campaign}:\uffff`)));
      for (const record of records) this.saved.set(record.item.id, record);
    }
    if (persist) await this.transaction(['catalogs', 'settings'], 'readwrite', tx => {
      tx.objectStore('catalogs').put(catalog, campaign);
      tx.objectStore('settings').put(campaign, 'lastCampaign');
    });
  }

  url(item) {
    const record = this.saved.get(item.id);
    if (!record) return '';
    if (!this.urls.has(item.id)) this.urls.set(item.id, URL.createObjectURL(record.blob));
    return this.urls.get(item.id);
  }

  async save(item) {
    const campaign = this.campaign;
    if (!campaign || !/^\/studio\/audio\/[a-f0-9]{32}\.wav$/.test(item.audio_url)) throw new Error('Audio introuvable.');
    const response = await fetch(item.audio_url, {cache: 'no-store'});
    if (response.status !== 200 || !response.headers.get('Content-Type')?.startsWith('audio/wav')) throw new Error('Téléchargement audio incomplet.');
    const blob = await response.blob();
    if (!blob.size || await blob.slice(0, 4).text() !== 'RIFF') throw new Error('Audio invalide.');
    const record = {item, blob};
    await this.transaction(['audio'], 'readwrite', tx => tx.objectStore('audio').put(record, `${campaign}:${item.id}`));
    if (campaign === this.campaign) this.saved.set(item.id, record);
    // A refusal is harmless; the download is already committed.
    if (navigator.storage?.persist) navigator.storage.persist().catch(() => {});
  }

  async remove(item) {
    const campaign = this.campaign;
    await this.transaction(['audio'], 'readwrite', tx => tx.objectStore('audio').delete(`${campaign}:${item.id}`));
    if (campaign !== this.campaign) return;
    this.saved.delete(item.id);
    if (this.urls.has(item.id)) URL.revokeObjectURL(this.urls.get(item.id));
    this.urls.delete(item.id);
  }
}
