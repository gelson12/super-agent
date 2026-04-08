import { motion } from "framer-motion";
import { useInView } from "framer-motion";
import { useRef } from "react";

const AboutSection = () => {
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: "-100px" });

  return (
    <section id="about" className="relative py-32" ref={ref}>
      <div className="container mx-auto px-6">
        <div className="mx-auto max-w-4xl">
          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={inView ? { opacity: 1, y: 0 } : {}}
            transition={{ duration: 0.8 }}
            className="mb-16 text-center"
          >
            <p className="mb-4 font-body text-xs tracking-[0.3em] text-primary uppercase">
              Who We Are
            </p>
            <h2 className="font-display text-3xl font-semibold text-foreground sm:text-4xl md:text-5xl">
              Where Business Meets{" "}
              <span className="text-gradient-gold">Modern Growth</span>
            </h2>
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={inView ? { opacity: 1, y: 0 } : {}}
            transition={{ duration: 0.8, delay: 0.2 }}
            className="grid gap-12 md:grid-cols-2"
          >
            <div className="space-y-6">
              <p className="font-body text-base leading-relaxed text-muted-foreground">
                Bridge is a business growth, technology, and media company that
                connects businesses, customers, and technology through digital
                marketing, software, automation, and visual media.
              </p>
              <p className="font-body text-base leading-relaxed text-muted-foreground">
                We help businesses modernise their presence, improve operations,
                and build stronger customer relationships through intelligent
                systems and creative execution.
              </p>
            </div>

            <div className="space-y-8">
              <div className="border-l-2 border-gold pl-6">
                <h3 className="mb-2 font-display text-lg font-semibold text-foreground">
                  Our Mission
                </h3>
                <p className="font-body text-sm leading-relaxed text-muted-foreground">
                  To bridge the gap between businesses, customers, and
                  technology — building modern systems, digital experiences, and
                  creative assets that drive growth with clarity, trust, and
                  lasting impact.
                </p>
              </div>
              <div className="border-l-2 border-gold pl-6">
                <h3 className="mb-2 font-display text-lg font-semibold text-foreground">
                  Our Vision
                </h3>
                <p className="font-body text-sm leading-relaxed text-muted-foreground">
                  To become the trusted force businesses rely on to evolve —
                  making technology, automation, and digital presence feel
                  practical, powerful, and profitable.
                </p>
              </div>
            </div>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default AboutSection;
