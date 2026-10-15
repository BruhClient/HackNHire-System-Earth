// map_eudr.js - the colour-coded EUDR tile layer. Tiles come from Python (eudr://tiles/...), drawn
// from the stencils stored in Neon. The legend and the loading banner are Qt widgets over the map.
addCss('.eudr-tiles img { image-rendering: pixelated; }');

var eudr = { layer: null, hidden: [], opacity: 0.7, visible: true, key: '' };

// The map's schema is in every tile URL, so a tile cached for one map is never shown for another.
function tileUrl() {
  return 'eudr://tiles/{z}/{x}/{y}.png?map=' + encodeURIComponent(eudr.key) + '&hide=' + eudr.hidden.join(',');
}

function setMapInfo(info) {
  eudr.key = info.schema || info.map_version;
  bounds = info.bounds;  // lock the view to the map stored in Neon
  map.setMaxBounds(bounds);
  aoi.setBounds(bounds);
  lockView(true);
  if (eudr.layer) map.removeLayer(eudr.layer);
  eudr.layer = L.tileLayer(tileUrl(), {
    bounds: bounds, opacity: eudr.opacity, maxZoom: 19, zIndex: 5,
    className: 'eudr-tiles', keepBuffer: 4
  });
  if (eudr.visible) eudr.layer.addTo(map);
}

// No map to show (none stored yet, or the one shown was deleted).
function clearMapInfo() {
  if (eudr.layer) map.removeLayer(eudr.layer);
  eudr.layer = null;
}

function setHiddenClasses(codes) {
  eudr.hidden = codes.slice();
  if (eudr.layer) eudr.layer.setUrl(tileUrl());
}
function setOverlayOpacity(value) { eudr.opacity = value; if (eudr.layer) eudr.layer.setOpacity(value); }
function setOverlayVisible(on) {
  eudr.visible = on;
  if (!eudr.layer) return;
  if (on) eudr.layer.addTo(map); else map.removeLayer(eudr.layer);
}
