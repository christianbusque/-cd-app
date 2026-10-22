/*
  Bouton « Installations » pour toutes les pages de CD Repère
  (rack, CD-Nesting, CD-Palette, planification, etc.).

  Pour l'ajouter à une page, coller cette ligne juste avant </body> :
    <script src="/bouton-installations.js"></script>

  Le bouton ouvre le calendrier des installations par-dessus la page,
  avec un gros bouton FERMER pour revenir (pas de nouvel onglet : sûr pour
  les écrans en mode kiosque comme celui du rack).

  Options (facultatives) sur la balise <script> :
    data-position="bas-gauche" | "bas-droite" (défaut) | "haut-droite" | "haut-gauche"
*/
(function () {
  if (window.__cdBoutonInstallations) return;
  window.__cdBoutonInstallations = true;

  var URL_PAGE = '/installations';
  var script = document.currentScript;
  var pos = (script && script.getAttribute('data-position')) || 'bas-droite';
  var coin = {
    'bas-droite': 'right:16px;bottom:16px', 'bas-gauche': 'left:16px;bottom:16px',
    'haut-droite': 'right:16px;top:16px', 'haut-gauche': 'left:16px;top:16px'
  }[pos] || 'right:16px;bottom:16px';

  function demarrer() {
    var style = document.createElement('style');
    style.textContent =
      '#cd-btn-inst{position:fixed;' + coin + ';z-index:2147483000;display:flex;align-items:center;gap:8px;' +
      'background:#F2A800;color:#111;border:0;border-radius:28px;padding:12px 18px;' +
      'font:600 16px system-ui,-apple-system,sans-serif;box-shadow:0 4px 14px rgba(0,0,0,.45);cursor:pointer}' +
      '#cd-btn-inst:active{transform:scale(.97)}' +
      '#cd-inst-voile{position:fixed;inset:0;z-index:2147483001;background:#0E1113;display:none;flex-direction:column}' +
      '#cd-inst-voile.on{display:flex}' +
      '#cd-inst-barre{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;' +
      'background:#181C1F;border-bottom:1px solid #282D31;font:600 16px system-ui,sans-serif;color:#E3C46A}' +
      '#cd-inst-fermer{background:#D93A3A;color:#fff;border:0;border-radius:8px;padding:12px 22px;' +
      'font:700 16px system-ui,sans-serif;cursor:pointer}' +
      '#cd-inst-cadre{flex:1;border:0;width:100%;background:#0E1113}';
    document.head.appendChild(style);

    var btn = document.createElement('button');
    btn.id = 'cd-btn-inst';
    btn.type = 'button';
    btn.innerHTML = '<span aria-hidden="true">&#128197;</span> Installations';

    var voile = document.createElement('div');
    voile.id = 'cd-inst-voile';
    voile.innerHTML =
      '<div id="cd-inst-barre"><span>Calendrier des installations</span>' +
      '<button id="cd-inst-fermer" type="button">FERMER &#10005;</button></div>';
    var cadre = null;

    btn.addEventListener('click', function () {
      if (!cadre) {
        cadre = document.createElement('iframe');
        cadre.id = 'cd-inst-cadre';
        cadre.title = 'Calendrier des installations';
        voile.appendChild(cadre);
      }
      // recharge à chaque ouverture pour avoir les dates à jour
      var large = window.innerWidth >= 1280 ? '?tv=1' : '';
      cadre.src = URL_PAGE + large + (large ? '&' : '?') + 't=' + Date.now();
      voile.classList.add('on');
      btn.style.display = 'none';
    });
    voile.addEventListener('click', function (e) {
      if (e.target && e.target.id === 'cd-inst-fermer') {
        voile.classList.remove('on');
        btn.style.display = '';
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && voile.classList.contains('on')) {
        voile.classList.remove('on');
        btn.style.display = '';
      }
    });

    document.body.appendChild(btn);
    document.body.appendChild(voile);
  }

  if (document.body) demarrer();
  else document.addEventListener('DOMContentLoaded', demarrer);
})();
