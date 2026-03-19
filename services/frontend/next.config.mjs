/** @type {import('next').NextConfig} */
const nextConfig = {
  // standalone = Docker minimal image; skip on Vercel (it uses its own bundler)
  output: process.env.VERCEL ? undefined : "standalone",

  // Serve the app under /aria when deployed to Vercel
  basePath: process.env.VERCEL ? "/aria" : "",

  experimental: {
    // Suppress "punycode" deprecation noise from some transitive deps
    serverComponentsExternalPackages: [],
  },
};

export default nextConfig;
