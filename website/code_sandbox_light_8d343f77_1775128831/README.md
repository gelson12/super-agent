# BRIDGE — Business Revenue Innovation Digital Growth Ecosystems

**Premium Website — Gold & Dark Luxury Theme**

## Overview

BRIDGE is a business growth, technology, and media company that connects businesses, customers, and technology through digital marketing, software, automation, and visual media.

**Tagline:** *"Bridging Business, Customers & Technology"*

---

## ✅ Completed Features

### Website Sections
- **Preloader** — Animated bridge SVG with gold loading bar
- **Navbar** — Fixed navigation with glass-morphism scroll effect, language switcher (EN/PT)
- **Hero Section** — Full-screen dark background with particle animation, animated stats, gold shimmer titles, CTA buttons
- **Marquee Ticker** — Scrolling service names with gold styling
- **About Section** — Company description, mission/vision cards, floating stat bubbles, animated bridge SVG
- **Services Section** — 5 service cards with 3D tilt hover effect, gold glow, feature lists
- **Why Bridge** — Problem/Solution comparison cards (4 pain points addressed)
- **Results Section** — Count-up stats (400+, 300%, 80%, 5x), 3 case study cards
- **Process Section** — 5-step visual process (Discovery → Strategy → Build → Launch → Grow)
- **Testimonials** — Auto-playing carousel with 5 testimonials (EN + PT), touch/swipe support
- **CTA Band** — Full-width call-to-action section
- **Contact Section** — Contact form with lead capture, info card, social links
- **Footer** — Full footer with nav links, social, newsletter signup, language switcher

### Visual & Animation Features
- Gold particle canvas with mouse interaction
- Smooth scroll reveal animations (fade up, left, right)
- Count-up number animations
- 3D card tilt effect on service cards
- Gold cursor glow effect
- Hero parallax on scroll
- Gold shimmer text animation
- Preloader animation with progress bar

### Functionality
- **Bilingual (EN/PT)** — Full language switching via data attributes
- **Language persistence** — Saved in localStorage
- **Contact form** — Saves leads to `bridge_leads` table via RESTful API
- **Newsletter signup** — Saves subscribers to `bridge_newsletter` table
- **Active nav link** — Highlights current section on scroll
- **Mobile responsive** — Full mobile/tablet/desktop support

---

## 📁 File Structure

```
index.html          — Main website HTML
css/
  style.css         — Full stylesheet (gold/dark luxury theme)
js/
  main.js           — Animations, carousel, language toggle, forms
README.md           — This file
```

---

## 🗄️ Data Storage

### Table: `bridge_leads`
Contact form submissions:
- `firstName`, `lastName`, `email`, `phone`
- `service` — Service of interest (marketing/software/ai/media/systems/all)
- `message` — Business description / challenge
- `language` — Language used (en/pt)
- `timestamp` — Submission time

### Table: `bridge_newsletter`
Newsletter subscribers:
- `email` — Subscriber email
- `timestamp` — Subscription time

**API Endpoints:**
- `GET tables/bridge_leads` — List all leads
- `POST tables/bridge_leads` — Submit new lead
- `GET tables/bridge_newsletter` — List subscribers
- `POST tables/bridge_newsletter` — Add subscriber

---

## 🎨 Design System

### Color Palette
| Token | Value | Usage |
|-------|-------|-------|
| Gold Deep | `#8B6914` | Shadows, gradients |
| Gold Mid | `#B8860B` | Primary gold |
| Gold Main | `#DAA520` | Interactive elements |
| Gold Bright | `#FFD700` | Highlights, glow |
| Dark BG | `#0A0A0A` | Main background |
| Dark 1 | `#111111` | Section alternation |
| Dark 2 | `#181818` | Cards |

### Typography
- **Headings:** Playfair Display (serif, elegant)
- **Subheadings:** Cormorant Garamond (italic quotes)
- **Body:** Inter (clean, readable)

---

## 🌍 Language Support
The site supports **English** and **Portuguese (PT)** via:
- `data-en` and `data-pt` attributes on text elements
- Language toggle buttons in navbar and footer
- Language preference persisted in `localStorage`

---

## 🚀 Services Featured
1. Digital & Social Marketing
2. Application Software
3. AI Agents & Automation *(Most Popular)*
4. Visual Media & Filmography
5. Business Systems

---

## 📋 Recommended Next Steps
1. Add real logo image files when available
2. Update contact email/phone with actual business details
3. Connect social media links
4. Add Google Analytics / Meta Pixel tracking
5. Configure email notifications for form submissions
6. Add case study detail pages
7. Integrate a booking calendar for consultations
8. Add blog/insights section
9. SEO meta tags and Open Graph images
10. Deploy via Publish tab
