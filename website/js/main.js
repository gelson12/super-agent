/* ============================================================
   BRIDGE — Main JavaScript
   Animations, Interactions, Canvas Particles
============================================================ */

'use strict';

/* ── PRELOADER ─────────────────────────────────────────── */
window.addEventListener('load', () => {
  setTimeout(() => {
    const preloader = document.getElementById('preloader');
    if (preloader) {
      preloader.classList.add('hidden');
      document.body.style.overflow = '';
    }
    // Trigger initial reveal animations
    runReveal();
  }, 2200);
  document.body.style.overflow = 'hidden';
});

/* ── NAVBAR SCROLL ─────────────────────────────────────── */
const navbar = document.getElementById('navbar');
let lastScroll = 0;

window.addEventListener('scroll', () => {
  const currentScroll = window.scrollY;

  if (currentScroll > 60) {
    navbar.classList.add('scrolled');
  } else {
    navbar.classList.remove('scrolled');
  }

  // Back to top
  const backToTop = document.getElementById('backToTop');
  if (backToTop) {
    if (currentScroll > 500) {
      backToTop.classList.add('visible');
    } else {
      backToTop.classList.remove('visible');
    }
  }

  lastScroll = currentScroll;
}, { passive: true });

/* ── MOBILE NAV ─────────────────────────────────────────── */
const hamburger = document.getElementById('hamburger');
const navLinks = document.getElementById('navLinks');

hamburger?.addEventListener('click', () => {
  hamburger.classList.toggle('open');
  navLinks.classList.toggle('open');
});

// Close on link click
document.querySelectorAll('.nav-link').forEach(link => {
  link.addEventListener('click', () => {
    hamburger?.classList.remove('open');
    navLinks?.classList.remove('open');
  });
});

/* ── SMOOTH ANCHOR SCROLL ─────────────────────────────── */
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
  anchor.addEventListener('click', e => {
    const target = document.querySelector(anchor.getAttribute('href'));
    if (target) {
      e.preventDefault();
      const navH = navbar ? navbar.offsetHeight : 80;
      const targetPos = target.getBoundingClientRect().top + window.scrollY - navH;
      window.scrollTo({ top: targetPos, behavior: 'smooth' });
    }
  });
});

/* ── BACK TO TOP ────────────────────────────────────────── */
document.getElementById('backToTop')?.addEventListener('click', () => {
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

/* ── PARTICLE CANVAS ─────────────────────────────────────── */
(function initParticles() {
  const canvas = document.getElementById('particleCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let W, H, particles = [], raf;

  const COLORS = [
    'rgba(218,165,32,', 'rgba(255,215,0,', 'rgba(184,134,11,',
    'rgba(240,192,64,', 'rgba(255,255,255,'
  ];

  function resize() {
    W = canvas.width = canvas.offsetWidth;
    H = canvas.height = canvas.offsetHeight;
  }

  function createParticle(x, y) {
    return {
      x: x ?? Math.random() * W,
      y: y ?? Math.random() * H,
      r: Math.random() * 1.8 + 0.3,
      vx: (Math.random() - 0.5) * 0.3,
      vy: -Math.random() * 0.5 - 0.1,
      alpha: Math.random() * 0.5 + 0.1,
      color: COLORS[Math.floor(Math.random() * COLORS.length)],
      life: 1,
      decay: Math.random() * 0.003 + 0.001
    };
  }

  function init() {
    particles = [];
    for (let i = 0; i < 80; i++) {
      const p = createParticle();
      p.life = Math.random();
      particles.push(p);
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);

    // Draw connections
    for (let i = 0; i < particles.length; i++) {
      for (let j = i + 1; j < particles.length; j++) {
        const dx = particles[i].x - particles[j].x;
        const dy = particles[i].y - particles[j].y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 120) {
          ctx.beginPath();
          ctx.strokeStyle = `rgba(184,134,11,${0.06 * (1 - dist / 120)})`;
          ctx.lineWidth = 0.5;
          ctx.moveTo(particles[i].x, particles[i].y);
          ctx.lineTo(particles[j].x, particles[j].y);
          ctx.stroke();
        }
      }
    }

    // Draw particles
    particles.forEach((p, idx) => {
      p.x += p.vx;
      p.y += p.vy;
      p.life -= p.decay;

      if (p.life <= 0 || p.y < -10) {
        particles[idx] = createParticle(Math.random() * W, H + 10);
        return;
      }

      const alpha = p.alpha * p.life;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `${p.color}${alpha})`;
      ctx.fill();
    });

    raf = requestAnimationFrame(draw);
  }

  resize();
  init();
  draw();

  window.addEventListener('resize', () => {
    resize();
    init();
  }, { passive: true });

  // Mouse interaction
  let mouseX = -1000, mouseY = -1000;
  document.getElementById('home')?.addEventListener('mousemove', e => {
    const rect = canvas.getBoundingClientRect();
    mouseX = e.clientX - rect.left;
    mouseY = e.clientY - rect.top;

    if (Math.random() > 0.7 && particles.length < 150) {
      particles.push(createParticle(mouseX + (Math.random() - 0.5) * 40, mouseY + (Math.random() - 0.5) * 40));
    }
  }, { passive: true });
})();

/* ── HERO STAT COUNTER ─────────────────────────────────── */
function animateCountUp(el, target, duration = 2000) {
  let start = 0;
  const step = target / (duration / 16);
  const timer = setInterval(() => {
    start += step;
    if (start >= target) {
      start = target;
      clearInterval(timer);
    }
    el.textContent = Math.floor(start).toLocaleString();
  }, 16);
}

let heroStatsAnimated = false;
function checkHeroStats() {
  if (heroStatsAnimated) return;
  const statsEl = document.querySelector('.hero-stats');
  if (!statsEl) return;
  const rect = statsEl.getBoundingClientRect();
  if (rect.top < window.innerHeight * 0.9) {
    heroStatsAnimated = true;
    document.querySelectorAll('.hero-stats .stat-number[data-target]').forEach(el => {
      animateCountUp(el, parseInt(el.dataset.target));
    });
  }
}

/* ── RESULTS COUNT UP ──────────────────────────────────── */
const countedEls = new Set();
function checkCountUps() {
  document.querySelectorAll('.count-up[data-target]').forEach(el => {
    if (countedEls.has(el)) return;
    const rect = el.getBoundingClientRect();
    if (rect.top < window.innerHeight * 0.9) {
      countedEls.add(el);
      animateCountUp(el, parseInt(el.dataset.target), 2000);
    }
  });
}

/* ── SCROLL REVEAL ──────────────────────────────────────── */
function runReveal() {
  const revealEls = document.querySelectorAll('.reveal-up, .reveal-left, .reveal-right');
  revealEls.forEach(el => {
    const rect = el.getBoundingClientRect();
    if (rect.top < window.innerHeight * 0.88) {
      const delay = el.dataset.delay ? parseInt(el.dataset.delay) : 0;
      setTimeout(() => el.classList.add('visible'), delay);
    }
  });
  checkHeroStats();
  checkCountUps();
}

window.addEventListener('scroll', runReveal, { passive: true });
window.addEventListener('resize', runReveal, { passive: true });

/* ── TESTIMONIALS CAROUSEL ──────────────────────────────── */
(function initTestimonials() {
  const track = document.getElementById('testimonialsTrack');
  const dotsContainer = document.getElementById('testiDots');
  const prevBtn = document.getElementById('prevTesti');
  const nextBtn = document.getElementById('nextTesti');

  if (!track) return;

  const cards = track.querySelectorAll('.testimonial-card');
  const total = cards.length;
  let current = 0;
  let autoPlayTimer;

  // Create dots
  cards.forEach((_, i) => {
    const dot = document.createElement('button');
    dot.className = `testi-dot${i === 0 ? ' active' : ''}`;
    dot.setAttribute('aria-label', `Testimonial ${i + 1}`);
    dot.addEventListener('click', () => goTo(i));
    dotsContainer?.appendChild(dot);
  });

  function goTo(index) {
    current = (index + total) % total;
    track.style.transform = `translateX(-${current * 100}%)`;
    document.querySelectorAll('.testi-dot').forEach((d, i) => {
      d.classList.toggle('active', i === current);
    });
  }

  function next() { goTo(current + 1); }
  function prev() { goTo(current - 1); }

  prevBtn?.addEventListener('click', () => { prev(); resetAuto(); });
  nextBtn?.addEventListener('click', () => { next(); resetAuto(); });

  function startAuto() {
    autoPlayTimer = setInterval(next, 5000);
  }
  function resetAuto() {
    clearInterval(autoPlayTimer);
    startAuto();
  }

  startAuto();

  // Touch/swipe support
  let touchStartX = 0;
  track.addEventListener('touchstart', e => { touchStartX = e.changedTouches[0].screenX; }, { passive: true });
  track.addEventListener('touchend', e => {
    const diff = touchStartX - e.changedTouches[0].screenX;
    if (Math.abs(diff) > 50) {
      diff > 0 ? next() : prev();
      resetAuto();
    }
  });
})();

/* ── LANGUAGE TOGGLE ────────────────────────────────────── */
let currentLang = 'en';

function applyLanguage(lang) {
  currentLang = lang;

  // Nav lang buttons
  document.querySelectorAll('.lang-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.lang === lang);
  });
  document.querySelectorAll('.lang-btn-footer').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.lang === lang);
  });

  // Elements with data-en / data-pt attributes
  document.querySelectorAll('[data-en]').forEach(el => {
    const text = el.getAttribute(`data-${lang}`);
    if (!text) return;

    // Check if text has HTML tags
    if (text.includes('<')) {
      el.innerHTML = text;
    } else {
      // Only update text, not children with their own translations
      if (el.children.length === 0 || el.dataset.forceText) {
        el.textContent = text;
      }
    }
  });

  // Section titles with innerHTML (gold spans)
  document.querySelectorAll('.section-title[data-en], .hero-title, h2[data-en]').forEach(el => {
    const text = el.getAttribute(`data-${lang}`);
    if (text && text.includes('<')) {
      el.innerHTML = text;
    }
  });

  // Store preference
  try { localStorage.setItem('bridge-lang', lang); } catch(e) {}
}

document.querySelectorAll('.lang-btn').forEach(btn => {
  btn.addEventListener('click', () => applyLanguage(btn.dataset.lang));
});
document.querySelectorAll('.lang-btn-footer').forEach(btn => {
  btn.addEventListener('click', () => applyLanguage(btn.dataset.lang));
});

// Load stored language preference
try {
  const storedLang = localStorage.getItem('bridge-lang');
  if (storedLang && ['en', 'pt'].includes(storedLang)) {
    applyLanguage(storedLang);
  }
} catch(e) {}

/* ── CONTACT FORM ───────────────────────────────────────── */
const contactForm = document.getElementById('contactForm');
const formSuccess = document.getElementById('formSuccess');
const submitBtn = document.getElementById('submitBtn');

contactForm?.addEventListener('submit', async (e) => {
  e.preventDefault();

  submitBtn.classList.add('loading');

  const formData = {
    firstName: contactForm.firstName.value,
    lastName: contactForm.lastName.value,
    email: contactForm.email.value,
    phone: contactForm.phone.value,
    service: contactForm.service.value,
    message: contactForm.message.value,
    language: currentLang,
    timestamp: new Date().toISOString()
  };

  try {
    // Save to table API
    await fetch('tables/bridge_leads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(formData)
    });
  } catch (err) {
    console.log('API save attempted');
  }

  // Simulate processing
  await new Promise(resolve => setTimeout(resolve, 1200));

  submitBtn.classList.remove('loading');
  formSuccess?.classList.add('show');
  contactForm.reset();

  // Hide success after 6s
  setTimeout(() => formSuccess?.classList.remove('show'), 6000);
});

/* ── NEWSLETTER FORM ────────────────────────────────────── */
document.getElementById('newsletterForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const emailInput = e.target.querySelector('input[type="email"]');
  const btn = e.target.querySelector('button');

  if (!emailInput.value) return;

  btn.innerHTML = '<i class="fas fa-check"></i>';
  btn.style.background = 'rgba(218,165,32,0.8)';

  try {
    await fetch('tables/bridge_newsletter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: emailInput.value, timestamp: new Date().toISOString() })
    });
  } catch (err) {}

  emailInput.value = '';
  setTimeout(() => {
    btn.innerHTML = '<i class="fas fa-arrow-right"></i>';
    btn.style.background = '';
  }, 3000);
});

/* ── FOOTER YEAR ────────────────────────────────────────── */
const yearEl = document.getElementById('year');
if (yearEl) yearEl.textContent = new Date().getFullYear();

/* ── ACTIVE NAV LINK on SCROLL ──────────────────────────── */
const sections = document.querySelectorAll('section[id]');
const navLinkEls = document.querySelectorAll('.nav-link:not(.nav-cta)');

function updateActiveNav() {
  const scrollPos = window.scrollY + (navbar?.offsetHeight || 80) + 40;
  sections.forEach(section => {
    const top = section.offsetTop;
    const bottom = top + section.offsetHeight;
    const id = section.getAttribute('id');
    const link = document.querySelector(`.nav-links a[href="#${id}"]`);
    if (scrollPos >= top && scrollPos < bottom && link) {
      navLinkEls.forEach(l => l.style.color = '');
      link.style.color = 'var(--gold-main)';
    }
  });
}
window.addEventListener('scroll', updateActiveNav, { passive: true });

/* ── SERVICE CARD HOVER TILT ────────────────────────────── */
document.querySelectorAll('.service-card').forEach(card => {
  card.addEventListener('mousemove', e => {
    const rect = card.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const cx = rect.width / 2;
    const cy = rect.height / 2;
    const rotX = ((y - cy) / cy) * -4;
    const rotY = ((x - cx) / cx) * 4;
    card.style.transform = `perspective(800px) rotateX(${rotX}deg) rotateY(${rotY}deg) translateY(-6px)`;
  });
  card.addEventListener('mouseleave', () => {
    card.style.transform = '';
  });
});

/* ── GOLD CURSOR GLOW ───────────────────────────────────── */
(function initCursorGlow() {
  const glow = document.createElement('div');
  glow.style.cssText = `
    position: fixed; pointer-events: none; z-index: 9998;
    width: 300px; height: 300px; border-radius: 50%;
    background: radial-gradient(circle, rgba(218,165,32,0.04) 0%, transparent 70%);
    transform: translate(-50%, -50%);
    transition: opacity 0.3s;
    opacity: 0;
  `;
  document.body.appendChild(glow);

  let mouseX = 0, mouseY = 0;
  let glowX = 0, glowY = 0;

  document.addEventListener('mousemove', e => {
    mouseX = e.clientX;
    mouseY = e.clientY;
    glow.style.opacity = '1';
  });

  document.addEventListener('mouseleave', () => {
    glow.style.opacity = '0';
  });

  function animateGlow() {
    glowX += (mouseX - glowX) * 0.08;
    glowY += (mouseY - glowY) * 0.08;
    glow.style.left = glowX + 'px';
    glow.style.top = glowY + 'px';
    requestAnimationFrame(animateGlow);
  }
  animateGlow();
})();

/* ── PARALLAX HERO ELEMENTS ─────────────────────────────── */
window.addEventListener('scroll', () => {
  const heroContent = document.querySelector('.hero-content');
  const heroMotif = document.querySelector('.hero-bg-motif');
  if (!heroContent || !heroMotif) return;
  const scrollY = window.scrollY;
  if (scrollY < window.innerHeight) {
    heroContent.style.transform = `translateY(${scrollY * 0.2}px)`;
    heroContent.style.opacity = `${1 - scrollY / (window.innerHeight * 0.8)}`;
    heroMotif.style.transform = `translateY(${scrollY * 0.1}px)`;
  }
}, { passive: true });

/* ── WHY CARD ENTRANCE ANIMATION ────────────────────────── */
function enhanceWhyCards() {
  document.querySelectorAll('.why-card').forEach(card => {
    const rect = card.getBoundingClientRect();
    if (rect.top < window.innerHeight * 0.85) {
      card.style.opacity = '1';
      card.style.transform = 'none';
    }
  });
}

// Init why cards hidden
document.querySelectorAll('.why-card').forEach((card, i) => {
  card.style.opacity = '0';
  card.style.transform = 'translateY(30px)';
  card.style.transition = `all 0.6s cubic-bezier(0,0,0.2,1) ${i * 0.1}s`;
});
window.addEventListener('scroll', enhanceWhyCards, { passive: true });

/* ── PROCESS STEP ANIMATION ─────────────────────────────── */
function animateProcessSteps() {
  document.querySelectorAll('.step-circle').forEach((circle, i) => {
    const rect = circle.getBoundingClientRect();
    if (rect.top < window.innerHeight * 0.85) {
      setTimeout(() => {
        circle.style.borderColor = 'var(--gold-main)';
        circle.style.boxShadow = '0 0 20px rgba(218,165,32,0.2)';
      }, i * 150);
    }
  });
}
window.addEventListener('scroll', animateProcessSteps, { passive: true });

/* ── INITIAL SETUP ──────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  // Set year
  const yearEl = document.getElementById('year');
  if (yearEl) yearEl.textContent = new Date().getFullYear();

  // Initialize select options text
  document.querySelectorAll('select option[data-en]').forEach(opt => {
    const lang = currentLang;
    const text = opt.getAttribute(`data-${lang}`);
    if (text) opt.textContent = text;
  });
});
