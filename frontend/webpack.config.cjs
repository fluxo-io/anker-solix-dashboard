const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { Compilation, sources } = require('webpack');
const designTokens = fs.readFileSync(require.resolve('@fluxo/design/tokens.json'));
const designTokensFilename = `design-tokens.${crypto.createHash('sha256').update(designTokens).digest('hex').slice(0, 12)}.json`;
class DesignTokensPlugin {
  apply(compiler) {
    compiler.hooks.thisCompilation.tap('DesignTokensPlugin', compilation => {
      compilation.hooks.processAssets.tap({name: 'DesignTokensPlugin', stage: Compilation.PROCESS_ASSETS_STAGE_ADDITIONAL}, () => {
        compilation.emitAsset(designTokensFilename, new sources.RawSource(designTokens));
      });
    });
  }
}
const MiniCssExtractPlugin = require('mini-css-extract-plugin');
const { WebpackManifestPlugin } = require('webpack-manifest-plugin');
module.exports = {
  entry: { dashboard: './src/dashboard.js', public: './src/public.js' },
  output: { path: path.resolve(__dirname, '../web/src/solarbank_dashboard/static/dist'), filename: '[name].[contenthash:12].js', assetModuleFilename: 'assets/[name].[contenthash:12][ext]', publicPath: '/static/dist/', clean: true },
  module: { rules: [
    { test: /\.css$/i, use: [MiniCssExtractPlugin.loader, 'css-loader'] },
    { test: /\.(woff2?|svg)$/i, type: 'asset/resource' }
  ] },
  plugins: [new DesignTokensPlugin(), new MiniCssExtractPlugin({filename:'[name].[contenthash:12].css'}), new WebpackManifestPlugin({
    publicPath: '',
    generate: (seed, files) => ({
      ...files.reduce((manifest, file) => ({...manifest, [file.name]: file.path}), seed),
      'design-tokens.json': designTokensFilename
    })
  })]
};
