import { motion, useInView } from "framer-motion";
import { useRef } from "react";
import { Monitor, Bot, Film, BarChart3, Code } from "lucide-react";

const services = [
  {
    icon: Monitor,
    title: "Digital & Social Marketing",
    description:
      "Strategy, content, and management across digital platforms to amplify your brand and reach the right audience.",
  },
  {
    icon: Code,
    title: "Application Software",
    description:
      "Custom-built software solutions designed to streamline business operations and unlock new capabilities.",
  },
  {
    icon: Bot,
    title: "AI Agents & Automation",
    description:
      "Intelligent systems that reduce manual work, improve efficiency, and let your team focus on what matters.",
  },
  {
    icon: Film,
    title: "Visual Media & Filmography",
    description:
      "Brand videos, commercial content, and creative visual assets that tell your story with impact.",
  },
  {
    icon: BarChart3,
    title: "Business Systems",
    description:
      "Sector-specific solutions for marketing, real estate, rent-to-rent, and construction businesses.",
  },
];

const ServicesSection = () => {
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: "-100px" });

  return (
    <section id="services" className="relative py-32" ref={ref}>
      {/* Subtle background accent */}
      <div className="absolute inset-0 bg-gradient-to-b from-transparent via-muted/30 to-transparent" />

      <div className="container relative z-10 mx-auto px-6">
        <motion.div
          initial={{ opacity: 0, y: 40 }}
          animate={inView ? { opacity: 1, y: 0 } : {}}
          transition={{ duration: 0.8 }}
          className="mb-16 text-center"
        >
          <p className="mb-4 font-body text-xs tracking-[0.3em] text-primary uppercase">
            What We Do
          </p>
          <h2 className="mx-auto max-w-2xl font-display text-3xl font-semibold text-foreground sm:text-4xl md:text-5xl">
            Services Built for{" "}
            <span className="text-gradient-gold">Growth</span>
          </h2>
        </motion.div>

        <div className="mx-auto grid max-w-6xl gap-6 md:grid-cols-2 lg:grid-cols-3">
          {services.map((service, i) => (
            <motion.div
              key={service.title}
              initial={{ opacity: 0, y: 40 }}
              animate={inView ? { opacity: 1, y: 0 } : {}}
              transition={{ duration: 0.6, delay: i * 0.1 }}
              className="group rounded-lg border border-gold bg-gradient-card p-8 shadow-card-elevated transition-all duration-300 hover:border-primary/50 hover:glow-gold"
            >
              <div className="mb-5 flex h-12 w-12 items-center justify-center rounded-md bg-primary/10">
                <service.icon className="h-6 w-6 text-primary" />
              </div>
              <h3 className="mb-3 font-display text-xl font-semibold text-foreground">
                {service.title}
              </h3>
              <p className="font-body text-sm leading-relaxed text-muted-foreground">
                {service.description}
              </p>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};

export default ServicesSection;
