// socket-client.js
// Include this file in public/mainadmin.html and optionally in public/index.html
// Requires: <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>

(function(){
  function qs(name){ return new URLSearchParams(location.search).get(name); }
  const token = qs('token') || '';
  if(typeof io === 'undefined') return;
  const socket = io({ auth: { token: token }, transports: ['websocket'] });

  socket.on('connect', () => console.log('socket connected', socket.id));
  socket.on('connect_error', (err) => console.error('socket connect_error', err));
  socket.on('new_topup', (data) => { console.log('new_topup', data); if(typeof loadTopups === 'function') loadTopups(); });
  socket.on('update_topup', (data) => { console.log('update_topup', data); if(typeof loadTopups === 'function') loadTopups(); });
  socket.on('new_withdraw', (data) => { console.log('new_withdraw', data); if(typeof loadWithdraws === 'function') loadWithdraws(); });
  socket.on('new_work', (data) => { console.log('new_work', data); if(typeof loadWorks === 'function') loadWorks(); });
  socket.on('new_topup_user', (data) => { alert('Заявка на пополнение принята'); try{ if(typeof renderMyRequests === 'function') renderMyRequests(); }catch(e){} });
  socket.on('new_work_user', (data) => { alert('Ваша заявка принята на проверку'); try{ if(typeof renderMyRequests === 'function') renderMyRequests(); }catch(e){} });
  socket.on('disconnect', (reason) => console.log('socket disconnected', reason));

  window.__RC_SOCKET = socket;
})();
