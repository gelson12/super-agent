import { motion, useInView } from "framer-motion";
import { useRef, useState } from "react";
import { ArrowRight, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";

const ContactSection = () => {
  const ref = useRef(null);
  const inView = useInView(ref, { once: true, margin: "-100px" });
  const [submitted, setSubmitted] = useState(false);
  const [loading, setLoading] = useState(false);
  const { toast } = useToast();

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoading(true);

    const formData = new FormData(e.currentTarget);
    const name = formData.get("name") as string;
    const email = formData.get("email") as string;
    const company = (formData.get("company") as string) || null;
    const message = formData.get("message") as string;

    try {
      const res = await fetch("https://super-agent-production.up.railway.app/contact", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, company, message, timestamp: new Date().toISOString() }),
      });
      if (!res.ok) throw new Error("Request failed");
      setSubmitted(true);
    } catch {
      toast({
        title: "Something went wrong",
        description: "Please try again later.",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <section id="contact" className="relative py-32" ref={ref}>
      <div className="container mx-auto px-6">
        <div className="mx-auto max-w-2xl">
          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={inView ? { opacity: 1, y: 0 } : {}}
            transition={{ duration: 0.8 }}
            className="mb-12 text-center"
          >
            <p className="mb-4 font-body text-xs tracking-[0.3em] text-primary uppercase">
              Get In Touch
            </p>
            <h2 className="font-display text-3xl font-semibold text-foreground sm:text-4xl md:text-5xl">
              Let's Build{" "}
              <span className="text-gradient-gold">Together</span>
            </h2>
            <p className="mx-auto mt-4 max-w-lg font-body text-base text-muted-foreground">
              Ready to modernise your business? Tell us about your project and
              we'll show you how Bridge can help you grow.
            </p>
          </motion.div>

          <motion.form
            initial={{ opacity: 0, y: 40 }}
            animate={inView ? { opacity: 1, y: 0 } : {}}
            transition={{ duration: 0.8, delay: 0.2 }}
            onSubmit={handleSubmit}
            className="space-y-5 rounded-lg border border-gold bg-gradient-card p-8 shadow-card-elevated"
          >
            {submitted ? (
              <div className="py-12 text-center">
                <p className="text-gradient-gold font-display text-2xl font-semibold">
                  Thank you
                </p>
                <p className="mt-2 font-body text-sm text-muted-foreground">
                  We'll be in touch shortly.
                </p>
              </div>
            ) : (
              <>
                <div className="grid gap-5 sm:grid-cols-2">
                  <input
                    type="text"
                    name="name"
                    placeholder="Name"
                    required
                    className="w-full rounded-sm border border-gold bg-muted px-4 py-3 font-body text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                  />
                  <input
                    type="email"
                    name="email"
                    placeholder="Email"
                    required
                    className="w-full rounded-sm border border-gold bg-muted px-4 py-3 font-body text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                  />
                </div>
                <input
                  type="text"
                  name="company"
                  placeholder="Company"
                  className="w-full rounded-sm border border-gold bg-muted px-4 py-3 font-body text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                />
                <textarea
                  name="message"
                  placeholder="Tell us about your project..."
                  rows={4}
                  required
                  className="w-full resize-none rounded-sm border border-gold bg-muted px-4 py-3 font-body text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                />
                <button
                  type="submit"
                  disabled={loading}
                  className="bg-gradient-gold flex w-full items-center justify-center gap-2 rounded-sm px-8 py-3.5 font-body text-sm font-semibold tracking-wider text-primary-foreground uppercase transition-opacity hover:opacity-90 disabled:opacity-60"
                >
                  {loading ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Sending...
                    </>
                  ) : (
                    <>
                      Send Message
                      <ArrowRight className="h-4 w-4" />
                    </>
                  )}
                </button>
              </>
            )}
          </motion.form>
        </div>
      </div>
    </section>
  );
};

export default ContactSection;
