/* Original-site effect configuration, using the locally bundled tsParticles engine.
 * Presentation only: no API calls, application state or teammate workbench changes.
 */
(() => {
  "use strict";
  const root = document.getElementById("ambient-network");
  if (!root) return;
  const color = "rgb(148, 163, 178)";
  const options = {
    autoPlay:true,
    background:{color:{value:"none"}},
    fullScreen:{enable:true, zIndex:0},
    detectRetina:true,
    fpsLimit:120,
    interactivity:{
      detectsOn:"window",
      events:{onClick:{enable:true, mode:"push"}, onHover:{enable:true, mode:"grab", parallax:{enable:true, force:60, smooth:10}}, resize:{enable:true, delay:.5}},
      modes:{grab:{distance:400, links:{opacity:1}}, push:{quantity:4}}
    },
    particles:{
      color:{value:color},
      collisions:{enable:false},
      move:{enable:true, speed:2, direction:"none", random:false, straight:false, outModes:{default:"out"}},
      number:{value:30, density:{enable:true, width:1920, height:1080}},
      opacity:{value:{min:.1,max:.5}, animation:{enable:true,speed:3,sync:false,startValue:"random"}},
      size:{value:{min:1,max:10}, animation:{enable:true,speed:20,sync:false,startValue:"random"}},
      shape:{type:"circle"},
      links:{enable:true, color, distance:150, opacity:.4, width:1}
    },
    pauseOnBlur:true,
    pauseOnOutsideViewport:true,
    motion:{disable:false, reduce:{factor:4,value:true}}
  };
  async function init() {
    await loadSlim(tsParticles);
    const container = await tsParticles.load({id:"ambient-network", options});
    if (!container) return;
    root.dataset.engine = "tsparticles-3.9.1";
    root.dataset.pushQuantity = "4";
    root.dataset.grabDistance = "400";
    const updateCount = () => { root.dataset.nodes = String(container.particles.count); };
    updateCount();
    addEventListener("click", () => setTimeout(updateCount,100), {passive:true});
    let paused = false;
    document.getElementById("motion-toggle")?.addEventListener("click", event => {
      paused = !paused;
      event.currentTarget.setAttribute("aria-pressed",String(paused));
      event.currentTarget.textContent = paused ? "开启背景动效" : "暂停背景动效";
      if (paused) container.pause(); else container.play();
    });
  }
  init().catch(() => { root.dataset.engine = "unavailable"; });
})();
