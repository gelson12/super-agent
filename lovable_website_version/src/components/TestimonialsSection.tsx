import { motion, useInView } from "framer-motion";
import { useRef, useState } from "react";
import { Star, Play, X } from "lucide-react";

const textTestimonials = [
  {
    name: "Sarah Mitchell",
    role: "CEO, Apex Logistics",
    quote:
      "Bridge transformed our legacy systems into a seamless digital platform. Revenue increased 40% within six months of launch.",
    stars: 5,
  },
  {
    name: "James Okonkwo",
    role: "Founder, GreenPath Ventures",
    quote:
      "The team at Bridge understood our vision from day one. Their strategic approach to digital transformation was exactly what we needed.",
    stars: 5,
  },
  {
    name: "Amara Chen",
    role: "COO, NovaTech Industries",
    quote:
      "Working with Bridge felt like having an in-house team. They delivered on time, on budget, and beyond expectations.",
    stars: 5,
  },
  {
    name: "David Onyango",
    role: "MD, Savannah Capital",
    quote:
      "Bridge's data analytics solutions gave us insights we never had before. Decision-making has never been this clear.",
    stars: 4,
  },
];

const videoTestimonials = [
  {
    name: "Liam Carter",
    role: "CTO, Skyline Solutions",
    thumbnail: "https://images.unsplash.com/photo-1560250097-0b93528c311a?w=600&h=400&fit=crop",
    videoUrl: "https://www.youtube.com/embed/dQw4w9WgXcQ",
  },
  {
    name: "Priya Naidoo",
    role: "Director, Meridian Group",
    thumbnail: "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?w=600&h=400&fit=crop",
    videoUrl: "https://www.youtube.com/embed/dQw4w9WgXcQ",
  },
];

const TestimonialsSection = () => {
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: "-100px" });
  const [activeVideo, setActiveVideo] = useState<string | null>(null);

  return (
    <section id="testimonials" className="relative py-32" ref={ref}>
      <div className="container mx-auto px-6">
        <motion.div
          initial={{ opacity: 0, y: 40 }}
          animate={inView ? { opacity: 1, y: 0 } : {}}
          transition={{ duration: 0.8 }}
          className="mb-16 text-center"
        >
          <p className="mb-4 font-body text-xs tracking-[0.3em] text-primary uppercase">
            Client Stories
          </p>
          <h2 className="font-display text-3xl font-semibold text-foreground sm:text-4xl md:text-5xl">
            Trusted by{" "}
            <span className="text-gradient-gold">Industry Leaders</span>
          </h2>
          <p className="mx-auto mt-4 max-w-lg font-body text-base text-muted-foreground">
            See how businesses across Africa and beyond have transformed with
            Bridge.
          </p>
        </motion.div>

        {/* Text Testimonials */}
        <div className="mb-20 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {textTestimonials.map((t, i) => (
            <motion.div
              key={t.name}
              initial={{ opacity: 0, y: 30 }}
              animate={inView ? { opacity: 1, y: 0 } : {}}
              transition={{ duration: 0.6, delay: 0.1 * i }}
              className="rounded-lg border border-gold bg-gradient-card p-6 shadow-card-elevated"
            >
              <div className="mb-4 flex gap-0.5">
                {Array.from({ length: 5 }).map((_, s) => (
                  <Star
                    key={s}
                    className={`h-4 w-4 ${
                      s < t.stars
                        ? "fill-primary text-primary"
                        : "text-muted-foreground"
                    }`}
                  />
                ))}
              </div>
              <p className="mb-6 font-body text-sm leading-relaxed text-muted-foreground">
                "{t.quote}"
              </p>
              <div>
                <p className="font-display text-sm font-semibold text-foreground">
                  {t.name}
                </p>
                <p className="font-body text-xs text-muted-foreground">
                  {t.role}
                </p>
              </div>
            </motion.div>
          ))}
        </div>

        {/* Video Testimonials */}
        <motion.div
          initial={{ opacity: 0, y: 40 }}
          animate={inView ? { opacity: 1, y: 0 } : {}}
          transition={{ duration: 0.8, delay: 0.4 }}
          className="mb-8 text-center"
        >
          <h3 className="font-display text-2xl font-semibold text-foreground sm:text-3xl">
            Hear It <span className="text-gradient-gold">First-Hand</span>
          </h3>
        </motion.div>

        <div className="mx-auto grid max-w-4xl gap-8 sm:grid-cols-2">
          {videoTestimonials.map((v, i) => (
            <motion.div
              key={v.name}
              initial={{ opacity: 0, y: 30 }}
              animate={inView ? { opacity: 1, y: 0 } : {}}
              transition={{ duration: 0.6, delay: 0.5 + 0.15 * i }}
              className="group cursor-pointer overflow-hidden rounded-lg border border-gold shadow-card-elevated"
              onClick={() => setActiveVideo(v.videoUrl)}
            >
              <div className="relative aspect-video overflow-hidden">
                <img
                  src={v.thumbnail}
                  alt={`${v.name} testimonial`}
                  className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-105"
                  loading="lazy"
                />
                <div className="absolute inset-0 flex items-center justify-center bg-background/40 transition-colors group-hover:bg-background/30">
                  <div className="flex h-16 w-16 items-center justify-center rounded-full bg-gradient-gold shadow-lg transition-transform group-hover:scale-110">
                    <Play className="h-7 w-7 fill-primary-foreground text-primary-foreground ml-1" />
                  </div>
                </div>
              </div>
              <div className="bg-gradient-card p-4">
                <p className="font-display text-sm font-semibold text-foreground">
                  {v.name}
                </p>
                <p className="font-body text-xs text-muted-foreground">
                  {v.role}
                </p>
              </div>
            </motion.div>
          ))}
        </div>
      </div>

      {/* Video Modal */}
      {activeVideo && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm"
          onClick={() => setActiveVideo(null)}
        >
          <div
            className="relative w-full max-w-3xl overflow-hidden rounded-lg border border-gold shadow-card-elevated"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={() => setActiveVideo(null)}
              className="absolute -top-10 right-0 text-foreground transition-colors hover:text-primary"
            >
              <X className="h-6 w-6" />
            </button>
            <div className="aspect-video">
              <iframe
                src={activeVideo + "?autoplay=1"}
                className="h-full w-full"
                allow="autoplay; encrypted-media"
                allowFullScreen
                title="Client testimonial video"
              />
            </div>
          </div>
        </div>
      )}
    </section>
  );
};

export default TestimonialsSection;
