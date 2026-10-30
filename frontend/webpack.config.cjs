const path = require('path');
const MiniCssExtractPlugin = require('mini-css-extract-plugin');
const { WebpackManifestPlugin } = require('webpack-manifest-plugin');
module.exports = {
  entry: { dashboard: './src/dashboard.js', public: './src/public.js' },
  output: { path: path.resolve(__dirname, '../web/src/solarbank_dashboard/static/dist'), filename: '[name].[contenthash:12].js', assetModuleFilename: 'assets/[name].[contenthash:12][ext]', publicPath: '/static/dist/', clean: true },
  module: { rules: [
    { test: /\.css$/i, use: [MiniCssExtractPlugin.loader, 'css-loader'] },
    { test: /\.(woff2?|svg)$/i, type: 'asset/resource' }
  ] },
  plugins: [new MiniCssExtractPlugin({filename:'[name].[contenthash:12].css'}), new WebpackManifestPlugin({publicPath:''})]
};
