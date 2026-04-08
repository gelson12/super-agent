import logo from "@/assets/logo.png";

const Footer = () => {
  return (
    <footer className="border-t border-gold py-16">
      <div className="container mx-auto px-6">
        <div className="flex flex-col items-center gap-8 text-center">
          <img src={logo} alt="Bridge" className="h-12 w-auto opacity-70" />
          <p className="max-w-md font-body text-sm text-muted-foreground">
            Bridging Business, Customers & Technology
          </p>
          <div className="flex gap-8">
            {["About", "Services", "Contact"].map((link) => (
              <a
                key={link}
                href={`#${link.toLowerCase()}`}
                className="font-body text-xs tracking-widest text-muted-foreground uppercase transition-colors hover:text-primary"
              >
                {link}
              </a>
            ))}
          </div>
          <div className="h-px w-full max-w-xs bg-gradient-to-r from-transparent via-primary/30 to-transparent" />
          <p className="font-body text-xs text-muted-foreground">
            © {new Date().getFullYear()} Bridge. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  );
};

export default Footer;
