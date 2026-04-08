import { motion } from "framer-motion";
import logo from "@/assets/logo.png";

const HeroSection = () => {
  return (
    <section className="relative flex min-h-screen items-center justify-center overflow-hidden bg-gradient-dark pt-20">
      {/* Subtle gold radial glow */}
      <div className="pointer-events-none absolute inset-0">
        <div className="absolute left-1/2 top-1/2 h-[600px] w-[600px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/5 blur-[120px]" />
      </div>

      {/* Thin gold line accents */}
      <div className="absolute left-0 top-0 h-px w-full bg-gradient-to-r from-transparent via-primary/30 to-transparent" />

      <div className="container relative z-10 mx-auto px-6 text-center">

        <motion.p
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.3, duration: 0.8 }}
          className="mb-4 font-body text-xs tracking-[0.35em] text-primary uppercase sm:text-sm"
        >
          Business Revenue Innovation Digital Growth Ecosystems
        </motion.p>

        <motion.h1
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.5, duration: 0.8 }}
          className="mx-auto mb-6 max-w-4xl font-display text-4xl font-semibold leading-tight text-foreground sm:text-5xl md:text-6xl lg:text-7xl"
        >
          Bridging Business,{" "}
          <span className="text-gradient-gold">Customers</span> &{" "}
          Technology
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.7, duration: 0.8 }}
          className="mx-auto mb-10 max-w-2xl font-body text-base leading-relaxed text-muted-foreground sm:text-lg"
        >
          We connect businesses, customers, and technology through digital
          marketing, software, automation, and visual media — driving growth
          with clarity, trust, and lasting impact.
        </motion.p>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.9, duration: 0.8 }}
          className="flex flex-col items-center justify-center gap-4 sm:flex-row"
        >
          <a
            href="#services"
            className="bg-gradient-gold rounded-sm px-10 py-3.5 font-body text-sm font-semibold tracking-wider text-primary-foreground uppercase transition-opacity hover:opacity-90"
          >
            Explore Services
          </a>
          <a
            href="#about"
            className="rounded-sm border border-gold px-10 py-3.5 font-body text-sm font-medium tracking-wider text-primary uppercase transition-colors hover:bg-primary/10"
          >
            Learn More
          </a>
        </motion.div>
      </div>

      {/* Bottom fade */}
      <div className="absolute bottom-0 left-0 right-0 h-32 bg-gradient-to-t from-background to-transparent" />
    </section>
  );
};

export default HeroSection;
