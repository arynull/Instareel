import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "IG Funnel - Admin Dashboard",
  description: "Manage Instagram content processing and posting.",
};

/**
 * Runs before hydration, right after next-themes' own anti-FOUC script:
 * next-themes restores `black` alone, but `dark` must ride along from the
 * first paint so every `dark:` utility applies (see DarkClassSync in
 * providers.tsx, which takes over after hydration).
 */
const blackThemeInit = `try{if(localStorage.getItem('theme')==='black')document.documentElement.classList.add('dark')}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-zinc-50 text-zinc-900 antialiased dark:bg-zinc-950 dark:text-zinc-100">
        <Providers>{children}</Providers>
        <script dangerouslySetInnerHTML={{ __html: blackThemeInit }} />
      </body>
    </html>
  );
}
