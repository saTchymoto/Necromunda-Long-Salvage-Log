/* A stand-in for Firebase (and for the Claude copy's database) used by the tests: an in-memory store with the same calls the log
   makes (collection, doc, onSnapshot, get, set, delete, runTransaction), a set delay per call (window.__lat, ms), and
   window.__remoteWrite(path, data) to play "someone else" saving. window.__echoFirst: the database echoes a save before it returns. */
(() => {
  const store = window.__store = {}; // path -> data
  const listeners = {}; // coll -> [fn]
  const LAT = () => window.__lat ?? 30;
  const clone = v => v == null ? v : JSON.parse(JSON.stringify(v));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const emit = (coll, changes) => { const all = Object.entries(store).filter(([p]) => p.split('/')[0] === coll).map(([p, d]) => mkdoc(p.split('/')[1], d)); (listeners[coll] || []).forEach(fn => fn({ docChanges: () => changes, docs: all })); };
  const mkdoc = (id, data) => ({ id, exists: data != null, data: () => clone(data) });
  window.__writes = [];
  /* window.__endListeners(coll, code): end every live listener on a collection, as the database does when its
     connection stops answering ('unavailable') or access is withdrawn ('revoked'). */
  window.__listeners = {};
  window.__endListeners = (coll, code) => { for (const L of window.__listeners[coll] || []) if (!L.dead) { L.dead = true; L.err && L.err({ code, message: 'ended by test' }); } };
  const write = (path, data) => {
    const [coll, id] = path.split('/'); const had = path in store;
    if (data == null) delete store[path]; else store[path] = clone(data);
    window.__writes.push([path, data == null ? null : clone(data)]);
    setTimeout(() => emit(coll, [{ type: data == null ? 'removed' : had ? 'modified' : 'added', doc: mkdoc(id, data) }]), 5);
  };
  window.__remoteWrite = write;
  const docRef = path => ({
    get: async () => { await sleep(LAT()); return mkdoc(path.split('/')[1], store[path]); },
    set: async d => { await sleep(LAT()); write(path, d); if (window.__echoFirst) await sleep(60); },
    delete: async () => { await sleep(LAT()); write(path, null); },
    _path: path
  });
  const query = (coll, filters = [], order = null, lim = 1e9) => ({
    where: (f, op, v) => query(coll, [...filters, [f, op, v]], order, lim),
    orderBy: (f, dir) => query(coll, filters, [f, dir], lim),
    limit: n => query(coll, filters, order, n),
    get: async () => { await sleep(LAT());
      let docs = Object.entries(store).filter(([p]) => p.split('/')[0] === coll).map(([p, d]) => mkdoc(p.split('/')[1], d));
      for (const [f, op, v] of filters) docs = docs.filter(d => op === '<' ? d.data()[f] < v : op === '>' ? d.data()[f] > v : d.data()[f] === v);
      if (order) docs.sort((a, b) => (a.data()[order[0]] > b.data()[order[0]] ? 1 : -1) * (order[1] === 'desc' ? -1 : 1));
      { const ds = docs.slice(0, lim); return { docs: ds, empty: !ds.length, size: ds.length }; } },
    onSnapshot: (fn, err) => { const L = { fn: s => { if (!L.dead) fn(s); }, err }; (listeners[coll] ||= []).push(L.fn); (window.__listeners[coll] ||= []).push(L);
      setTimeout(() => { const all = Object.entries(store).filter(([p]) => p.split('/')[0] === coll).map(([p, d]) => mkdoc(p.split('/')[1], d));
        /* window.__pagedNext[coll]: this listener delivers in two pages, as the Claude database may: a partial first page
           flagged fromCache, then the rest. */
        if ((window.__pagedNext || {})[coll]) { delete window.__pagedNext[coll]; const half = all.slice(0, Math.ceil(all.length / 3)), rest = all.slice(half.length);
          L.fn({ docs: half, metadata: { fromCache: true }, docChanges: () => half.map(doc => ({ type: 'added', doc })) });
          setTimeout(() => L.fn({ docs: all, metadata: { fromCache: false }, docChanges: () => rest.map(doc => ({ type: 'added', doc })) }), 500);
          return; }
        L.fn({ docs: all, docChanges: () => all.map(doc => ({ type: 'added', doc })) }); }, 20);
      return () => { L.dead = true; }; }
  });
  const fs = {
    enablePersistence: async () => {},
    collection: c => query(c),
    doc: p => docRef(p),
    runTransaction: async fn => { const t = { get: async r => { await sleep(LAT()); return mkdoc(r._path.split('/')[1], store[r._path]); }, sets: [], set(r, d) { this.sets.push([r._path, d]); } };
      await fn(t); await sleep(LAT()); for (const [p, d] of t.sets) write(p, d); }
  };
  const user = { displayName: 'Tester', email: 't@example.com' };
  const auth = { currentUser: user, onAuthStateChanged: cb => setTimeout(() => cb(user), 0), signInWithPopup: async () => {}, signOut: async () => {} };
  const authFn = () => auth; authFn.GoogleAuthProvider = function () { this.setCustomParameters = () => {}; };
  window.firebase = { initializeApp: () => {}, auth: authFn, firestore: () => fs };
})();
